"""Task and event-log store for xclaw orchestration state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Mapping, Sequence

from . import protocol as constants
from .markdown import MarkdownDocument, read_markdown_file, write_markdown_file
from .models import TaskContext, TaskEvent
from .protocol import Stage, TaskStatus
from .workspace import event_log_required_sections, task_required_sections

_SECTION_PATTERN = re.compile(
    r"^##\s+(?P<title>[^\n]+)\n(?P<body>.*?)(?=^##\s+|\Z)",
    flags=re.MULTILINE | re.DOTALL,
)
_BULLET_KEY_VALUE_PATTERN = re.compile(r"^-\s+([A-Za-z0-9_]+):\s*(.*)$")
_BULLET_PATTERN = re.compile(r"^-\s+(.*)$")

_PLACEHOLDER_VALUES: tuple[str, ...] = ("-", "null", "none", "")
_REPAIR_LOOP_ACTIONS: tuple[str, ...] = ("repair_loop_incremented",)
_EVENT_RESULT_NOTES_SEPARATOR = "; notes="


class TaskStoreError(RuntimeError):
    """Base error for task store operations."""


class TaskStoreContractError(TaskStoreError):
    """Raised when task.md or event_log.md content violates expected contracts."""


@dataclass
class _TaskRecord:
    document: MarkdownDocument
    task_id: str
    task_workspace_path: str
    bootstrap_plan_source_path: str | None
    target_repo_path: str
    target_repo_git_root: str | None
    target_repo_head: str | None
    target_repo_dirty: bool
    current_stage: Stage
    status: str
    current_owner: str
    gateway_pid: int | None
    current_artifacts: dict[str, str]
    recovery_notes: tuple[str, ...]


class TaskStore:
    """Read/update helpers for task state and append-only event rows."""

    def __init__(self, task_workspace_path: str | Path) -> None:
        self.task_workspace_path = Path(task_workspace_path).expanduser().resolve()
        self.task_file_path = self.task_workspace_path / constants.TASK_FILENAME
        self.event_log_file_path = self.task_workspace_path / constants.EVENT_LOG_FILENAME

        if not self.task_file_path.is_file():
            raise TaskStoreError(f"task.md is missing: {self.task_file_path}")
        if not self.event_log_file_path.is_file():
            raise TaskStoreError(f"event_log.md is missing: {self.event_log_file_path}")

    def load_task_context(self) -> TaskContext:
        """Load canonical task context from task.md + event_log.md."""

        record = self._read_task_record()
        events = self.list_events()

        return TaskContext(
            task_id=record.task_id,
            task_workspace_path=record.task_workspace_path,
            bootstrap_plan_source_path=record.bootstrap_plan_source_path,
            target_repo_path=record.target_repo_path,
            target_repo_git_root=record.target_repo_git_root,
            target_repo_head=record.target_repo_head,
            target_repo_dirty=record.target_repo_dirty,
            current_stage=record.current_stage,
            status=record.status,
            current_owner=record.current_owner,
            current_artifacts=record.current_artifacts,
            latest_event_seq=events[-1].seq if events else 0,
            repair_loop_count=_count_repair_loops(events),
            recovery_notes=record.recovery_notes,
            gateway_pid=record.gateway_pid,
        )

    def update_runtime_state(
        self,
        *,
        stage: Stage | str | None = None,
        status: TaskStatus | str | None = None,
        current_owner: str | None = None,
        gateway_pid: int | None | object = ...,
    ) -> TaskContext:
        """Update runtime state persisted in task.md."""

        record = self._read_task_record()
        if stage is not None:
            record.current_stage = Stage(stage)
        if status is not None:
            record.status = TaskStatus(status).value
        if current_owner is not None:
            normalized_owner = current_owner.strip()
            if normalized_owner not in (*constants.ROLE_NAMES, "human", "system"):
                allowed = ", ".join((*constants.ROLE_NAMES, "human", "system"))
                raise ValueError(f"current_owner must be one of: {allowed}.")
            record.current_owner = normalized_owner
        if gateway_pid is not ...:
            if gateway_pid is None:
                record.gateway_pid = None
            else:
                if not isinstance(gateway_pid, int):
                    raise TypeError("gateway_pid must be int or None.")
                if gateway_pid <= 0:
                    raise ValueError("gateway_pid must be > 0 when provided.")
                record.gateway_pid = gateway_pid
        self._write_task_record(record, bump_version=True)
        return self.load_task_context()

    def switch_stage_owner(
        self,
        *,
        stage: Stage | str,
        owner: str,
        status: TaskStatus | str | None = None,
    ) -> TaskContext:
        """Compatibility helper for switching stage/owner and optional status."""

        return self.update_runtime_state(
            stage=stage,
            current_owner=owner,
            status=status,
        )

    def set_current_artifact(
        self,
        *,
        artifact_type: str,
        artifact_path: str,
    ) -> TaskContext:
        """Update one current artifact pointer in task.md."""

        normalized_type = artifact_type.strip()
        if normalized_type not in constants.ARTIFACT_TYPES:
            allowed = ", ".join(constants.ARTIFACT_TYPES)
            raise ValueError(f"artifact_type must be one of: {allowed}.")

        normalized_path = artifact_path.strip()
        if not normalized_path:
            raise ValueError("artifact_path must be non-empty.")

        record = self._read_task_record()
        record.current_artifacts[normalized_type] = normalized_path
        self._write_task_record(record, bump_version=True)
        return self.load_task_context()

    def append_recovery_note(self, note: str) -> TaskContext:
        """Append one recovery note entry in task.md."""

        normalized_note = note.strip()
        if not normalized_note:
            raise ValueError("note must be non-empty.")

        record = self._read_task_record()
        record.recovery_notes = (*record.recovery_notes, normalized_note)
        self._write_task_record(record, bump_version=True)
        return self.load_task_context()

    def read_recovery_notes(self) -> tuple[str, ...]:
        """Read recovery notes from task.md."""

        return self._read_task_record().recovery_notes

    def list_events(self) -> tuple[TaskEvent, ...]:
        """Read all event rows from event_log.md in sequence order."""

        document = read_markdown_file(
            self.event_log_file_path,
            required_sections=event_log_required_sections(),
        )
        event_section = _extract_single_section(document.body, "Event Log")
        if event_section is None:
            raise TaskStoreContractError("event_log.md is missing 'Event Log' section.")

        headers, rows = _parse_markdown_table(event_section)
        expected_headers = (
            "seq",
            "timestamp",
            "actor",
            "action",
            "input_artifacts",
            "output_artifacts",
            "result",
        )
        if headers != expected_headers:
            raise TaskStoreContractError(
                "event_log.md table header mismatch. "
                f"Expected {expected_headers}, got {headers}.",
            )

        events: list[TaskEvent] = []
        for row in rows:
            if len(row) != len(expected_headers):
                raise TaskStoreContractError(
                    f"event_log.md row has {len(row)} columns, expected {len(expected_headers)}.",
                )
            payload = dict(zip(expected_headers, row))
            result, notes = _decode_event_result(payload["result"])
            try:
                seq = int(payload["seq"])
            except ValueError as exc:
                raise TaskStoreContractError(
                    f"event_log.md seq must be integer, got {payload['seq']!r}.",
                ) from exc

            try:
                event = TaskEvent(
                    seq=seq,
                    timestamp=payload["timestamp"],
                    actor=payload["actor"],
                    action=payload["action"],
                    input_artifacts=_parse_artifact_cell(payload["input_artifacts"]),
                    output_artifacts=_parse_artifact_cell(payload["output_artifacts"]),
                    result=result,
                    notes=notes,
                )
            except (TypeError, ValueError) as exc:
                raise TaskStoreContractError(
                    f"event_log.md row {seq} is invalid: {exc}",
                ) from exc
            events.append(event)

        self._validate_event_sequence(events)
        return tuple(events)

    def append_event(
        self,
        *,
        actor: str,
        action: str,
        input_artifacts: Sequence[str] = (),
        output_artifacts: Sequence[str] = (),
        result: str = "ok",
        notes: str | None = None,
        timestamp: str | None = None,
    ) -> TaskEvent:
        """Append one event row and keep sequence strictly increasing."""

        existing_events = list(self.list_events())
        next_seq = existing_events[-1].seq + 1 if existing_events else 1
        next_timestamp = timestamp or _utc_now_iso()

        event = TaskEvent(
            seq=next_seq,
            timestamp=next_timestamp,
            actor=actor,
            action=action,
            input_artifacts=tuple(input_artifacts),
            output_artifacts=tuple(output_artifacts),
            result=result,
            notes=notes,
        )
        existing_events.append(event)
        self._write_event_log(tuple(existing_events), bump_version=True)
        return event

    def read_repair_loop_count(self) -> int:
        """Count completed tester/qa repair-loop route-back events."""

        return _count_repair_loops(self.list_events())

    def _read_task_record(self) -> _TaskRecord:
        document = read_markdown_file(
            self.task_file_path,
            required_sections=task_required_sections(),
        )
        sections = _extract_sections(document.body)

        basic_info_rows = _parse_table_pairs(sections.get("Basic Info"), "Basic Info")
        runtime_state_fields = _parse_bullet_key_values(
            sections.get("Runtime State"),
            "Runtime State",
        )
        artifact_rows = _parse_table_pairs(
            sections.get("Current Artifacts"),
            "Current Artifacts",
            allow_unknown_keys=True,
        )
        recovery_notes = _parse_bullet_items(
            sections.get("Recovery Notes"),
            "Recovery Notes",
        )

        try:
            current_stage = Stage(_normalize_required_text(runtime_state_fields.get("stage"), "stage"))
        except ValueError as exc:
            raise TaskStoreContractError("Invalid stage in task.md.") from exc

        try:
            status_value = TaskStatus(_normalize_required_text(runtime_state_fields.get("status"), "status")).value
        except ValueError as exc:
            allowed = ", ".join(constants.TASK_STATUS_NAMES)
            raise TaskStoreContractError(
                f"task.md status must be one of: {allowed}.",
            ) from exc

        owner_value = _normalize_required_text(runtime_state_fields.get("current_owner"), "current_owner")
        if owner_value not in (*constants.ROLE_NAMES, "human", "system"):
            raise TaskStoreContractError(f"Invalid current_owner in task.md: {owner_value!r}.")

        gateway_pid = _parse_optional_pid(runtime_state_fields.get("gateway_pid"))

        task_id_value = _normalize_required_text(
            basic_info_rows.get("task_id"),
            "task_id",
        )
        workspace_path = _normalize_required_text(
            basic_info_rows.get("task_workspace_path"),
            "task_workspace_path",
        )
        bootstrap_plan_source_path = _normalize_optional_text(
            basic_info_rows.get("bootstrap_plan_source_path")
        )
        target_repo_path = _normalize_required_text(
            basic_info_rows.get("target_repo_path"),
            "target_repo_path",
        )

        target_repo_git_root = _normalize_optional_text(basic_info_rows.get("target_repo_git_root"))
        target_repo_head = _normalize_optional_text(basic_info_rows.get("target_repo_head"))
        target_repo_dirty = _parse_bool_cell(basic_info_rows.get("target_repo_dirty"))

        current_artifacts: dict[str, str] = {}
        for artifact_type, raw_path in artifact_rows.items():
            normalized_path = _normalize_optional_text(raw_path)
            if normalized_path is None:
                continue
            if artifact_type not in constants.ARTIFACT_TYPES:
                continue
            current_artifacts[artifact_type] = normalized_path

        return _TaskRecord(
            document=document,
            task_id=task_id_value,
            task_workspace_path=workspace_path,
            bootstrap_plan_source_path=bootstrap_plan_source_path,
            target_repo_path=target_repo_path,
            target_repo_git_root=target_repo_git_root,
            target_repo_head=target_repo_head,
            target_repo_dirty=target_repo_dirty,
            current_stage=current_stage,
            status=status_value,
            current_owner=owner_value,
            gateway_pid=gateway_pid,
            current_artifacts=current_artifacts,
            recovery_notes=recovery_notes,
        )

    def _write_task_record(self, record: _TaskRecord, *, bump_version: bool) -> None:
        front_matter = dict(record.document.front_matter)
        version_value = _normalize_version(front_matter.get("version"))

        front_matter["task_id"] = record.task_id
        front_matter["artifact_type"] = "task"
        front_matter["stage"] = record.current_stage.value
        front_matter["producer"] = constants.ROLE_ORCHESTRATOR
        front_matter["consumer"] = constants.ROLE_ORCHESTRATOR
        front_matter["status"] = record.status
        front_matter["target_repo_path"] = record.target_repo_path
        front_matter["created_at"] = _utc_now_iso()

        if bump_version:
            front_matter["version"] = version_value + 1
            front_matter["supersedes"] = f"task.md@v{version_value}"
        else:
            front_matter["version"] = version_value
            front_matter["supersedes"] = front_matter.get("supersedes")

        body = _render_task_body(record)
        write_markdown_file(
            self.task_file_path,
            front_matter=front_matter,
            body=body,
            required_sections=task_required_sections(),
            create_parent=False,
        )

    def _write_event_log(self, events: tuple[TaskEvent, ...], *, bump_version: bool) -> None:
        document = read_markdown_file(
            self.event_log_file_path,
            required_sections=event_log_required_sections(),
        )
        record = self._read_task_record()
        front_matter = dict(document.front_matter)
        version_value = _normalize_version(front_matter.get("version"))

        front_matter["task_id"] = record.task_id
        front_matter["artifact_type"] = "event_log"
        front_matter["stage"] = record.current_stage.value
        front_matter["producer"] = constants.ROLE_ORCHESTRATOR
        front_matter["consumer"] = constants.ROLE_ORCHESTRATOR
        front_matter["status"] = record.status
        front_matter["target_repo_path"] = record.target_repo_path
        front_matter["created_at"] = _utc_now_iso()

        if bump_version:
            front_matter["version"] = version_value + 1
            front_matter["supersedes"] = f"event_log.md@v{version_value}"
        else:
            front_matter["version"] = version_value

        body = _render_event_log_body(events)
        write_markdown_file(
            self.event_log_file_path,
            front_matter=front_matter,
            body=body,
            required_sections=event_log_required_sections(),
            create_parent=False,
        )

    @staticmethod
    def _validate_event_sequence(events: Sequence[TaskEvent]) -> None:
        expected = 1
        for event in events:
            if event.seq != expected:
                raise TaskStoreContractError(
                    f"event_log.md sequence must be continuous from 1, got {event.seq} at {expected}.",
                )
            expected += 1


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _count_repair_loops(events: Sequence[TaskEvent]) -> int:
    count = 0
    for event in events:
        if event.action.strip().lower() in _REPAIR_LOOP_ACTIONS:
            count += 1
    return count


def _normalize_required_text(value: str | None, field_name: str) -> str:
    normalized = (value or "").strip()
    if not normalized:
        raise TaskStoreContractError(f"{field_name} must be non-empty in task.md.")
    return normalized


def _normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if normalized.lower() in _PLACEHOLDER_VALUES:
        return None
    return normalized


def _parse_bool_cell(value: str | None) -> bool:
    normalized = (value or "").strip().lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no", "-", "null", "", "none"}:
        return False
    raise TaskStoreContractError(f"target_repo_dirty must be bool-like, got {value!r}.")


def _parse_optional_pid(value: str | None) -> int | None:
    normalized = _normalize_optional_text(value)
    if normalized is None:
        return None
    if not normalized.isdigit():
        raise TaskStoreContractError(f"gateway_pid must be an integer or placeholder, got {value!r}.")
    pid = int(normalized)
    if pid <= 0:
        raise TaskStoreContractError("gateway_pid must be > 0 when provided.")
    return pid


def _normalize_version(value: object) -> int:
    if isinstance(value, int):
        if value < 1:
            raise TaskStoreContractError("front matter version must be >= 1.")
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            numeric = int(stripped)
            if numeric >= 1:
                return numeric
    raise TaskStoreContractError(f"front matter version is invalid: {value!r}.")


def _extract_sections(body: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    for match in _SECTION_PATTERN.finditer(body):
        title = match.group("title").strip()
        content = match.group("body").strip("\n")
        sections[title] = content
    return sections


def _extract_single_section(body: str, title: str) -> str | None:
    lines = body.splitlines()
    start_index: int | None = None

    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("#"):
            continue
        heading = stripped.lstrip("#").strip()
        if heading == title:
            start_index = index + 1
            break

    if start_index is None:
        return None
    return "\n".join(lines[start_index:]).strip("\n")


def _parse_markdown_table(section: str) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]:
    lines = [line.strip() for line in section.splitlines() if line.strip().startswith("|")]
    if len(lines) < 2:
        raise TaskStoreContractError("Markdown table must contain header and separator rows.")

    header = _split_table_row(lines[0])
    separator = _split_table_row(lines[1])
    if len(separator) != len(header) or any(not cell.replace("-", "") == "" for cell in separator):
        raise TaskStoreContractError("Markdown table separator row is invalid.")

    rows: list[tuple[str, ...]] = []
    for line in lines[2:]:
        cells = _split_table_row(line)
        if len(cells) != len(header):
            raise TaskStoreContractError(
                f"Markdown table row has {len(cells)} columns; expected {len(header)}.",
            )
        rows.append(cells)
    return header, tuple(rows)


def _split_table_row(line: str) -> tuple[str, ...]:
    text = line.strip()
    if not (text.startswith("|") and text.endswith("|")):
        raise TaskStoreContractError(f"Invalid table row: {line!r}")
    cells = [cell.strip() for cell in text[1:-1].split("|")]
    return tuple(cells)


def _parse_table_pairs(
    section: str | None,
    section_name: str,
    *,
    allow_unknown_keys: bool = False,
) -> dict[str, str]:
    if section is None:
        raise TaskStoreContractError(f"task.md is missing section: {section_name}.")

    _, rows = _parse_markdown_table(section)
    pairs: dict[str, str] = {}
    for row in rows:
        if len(row) < 2:
            raise TaskStoreContractError(
                f"Section {section_name} table row must have at least 2 columns.",
            )
        key = row[0].strip()
        value = row[1].strip()
        if not key:
            continue
        if not allow_unknown_keys and key in pairs:
            raise TaskStoreContractError(f"Duplicate key {key!r} in section {section_name}.")
        pairs[key] = value
    return pairs


def _parse_bullet_key_values(section: str | None, section_name: str) -> dict[str, str]:
    if section is None:
        raise TaskStoreContractError(f"task.md is missing section: {section_name}.")

    values: dict[str, str] = {}
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        match = _BULLET_KEY_VALUE_PATTERN.match(stripped)
        if match is None:
            raise TaskStoreContractError(
                f"Section {section_name} line must be '- key: value', got {line!r}.",
            )
        values[match.group(1)] = match.group(2).strip()
    return values


def _parse_bullet_items(section: str | None, section_name: str) -> tuple[str, ...]:
    if section is None:
        raise TaskStoreContractError(f"task.md is missing section: {section_name}.")

    values: list[str] = []
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        match = _BULLET_PATTERN.match(stripped)
        if match is None:
            raise TaskStoreContractError(
                f"Section {section_name} line must be '- value', got {line!r}.",
            )
        value = _normalize_optional_text(match.group(1))
        if value is not None:
            values.append(value)
    return tuple(values)


def _parse_artifact_cell(value: str) -> tuple[str, ...]:
    normalized = value.strip()
    if normalized.lower() in _PLACEHOLDER_VALUES:
        return ()
    parts = [item.strip() for item in normalized.split(",") if item.strip()]
    return tuple(parts)


def _encode_event_result(result: str, notes: str | None) -> str:
    normalized_result = result.strip()
    if not normalized_result:
        raise ValueError("result must be non-empty.")
    if notes is None:
        return normalized_result
    normalized_notes = notes.strip()
    if not normalized_notes:
        return normalized_result
    return f"{normalized_result}{_EVENT_RESULT_NOTES_SEPARATOR}{normalized_notes}"


def _decode_event_result(cell_value: str) -> tuple[str, str | None]:
    raw = cell_value.strip()
    if _EVENT_RESULT_NOTES_SEPARATOR not in raw:
        return raw, None
    result, notes = raw.split(_EVENT_RESULT_NOTES_SEPARATOR, maxsplit=1)
    return result.strip(), notes.strip() or None


def _render_task_body(record: _TaskRecord) -> str:
    lines: list[str] = [
        "# Task State",
        "",
        "## Basic Info",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| task_id | {record.task_id} |",
        f"| task_workspace_path | {record.task_workspace_path} |",
        f"| bootstrap_plan_source_path | {_display_optional(record.bootstrap_plan_source_path)} |",
        f"| target_repo_path | {record.target_repo_path} |",
        f"| target_repo_git_root | {_display_optional(record.target_repo_git_root)} |",
        f"| target_repo_head | {_display_optional(record.target_repo_head)} |",
        f"| target_repo_dirty | {'true' if record.target_repo_dirty else 'false'} |",
        "",
        "## Runtime State",
        "",
        f"- stage: {record.current_stage.value}",
        f"- status: {record.status}",
        f"- current_owner: {record.current_owner}",
        f"- gateway_pid: {record.gateway_pid if record.gateway_pid is not None else '-'}",
        "",
        "## Current Artifacts",
        "",
        "| artifact_type | artifact_path |",
        "| --- | --- |",
    ]

    for artifact_type in sorted(record.current_artifacts):
        lines.append(f"| {artifact_type} | {record.current_artifacts[artifact_type]} |")

    lines.extend(
        [
            "",
            "## Recovery Notes",
            "",
        ],
    )

    if record.recovery_notes:
        for note in record.recovery_notes:
            lines.append(f"- {note}")
    else:
        lines.append("- -")

    return "\n".join(lines) + "\n"


def _render_event_log_body(events: Sequence[TaskEvent]) -> str:
    lines: list[str] = [
        "# Event Log",
        "",
        "| seq | timestamp | actor | action | input_artifacts | output_artifacts | result |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]

    for event in events:
        lines.append(
            "| "
            + " | ".join(
                (
                    str(event.seq),
                    event.timestamp,
                    event.actor,
                    event.action,
                    _format_artifact_cell(event.input_artifacts),
                    _format_artifact_cell(event.output_artifacts),
                    _encode_event_result(event.result, event.notes),
                ),
            )
            + " |",
        )

    return "\n".join(lines) + "\n"


def _format_artifact_cell(artifacts: Sequence[str]) -> str:
    if not artifacts:
        return "-"
    return ",".join(item.strip() for item in artifacts if item.strip())


def _display_optional(value: str | None) -> str:
    if value is None:
        return "-"
    normalized = value.strip()
    if not normalized:
        return "-"
    return normalized


__all__ = [
    "TaskStore",
    "TaskStoreContractError",
    "TaskStoreError",
]
