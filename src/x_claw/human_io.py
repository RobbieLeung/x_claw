"""Human supervision helpers for the simplified x_claw runtime."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re

from . import protocol as constants
from .artifact_store import ArtifactStore
from .protocol import HumanAdviceDisposition, ReviewDecision, Stage, TaskStatus, fixed_next_stage, owner_for_stage
from .task_store import TaskStore

_SUMMARY_FIELD_PATTERN = r"^-\s*{field}:\s*(.+)$"
_ADVICE_SECTION_RE = re.compile(r"^###\s+(?P<advice_id>[^\n]+)\n(?P<body>.*?)(?=^###\s+|\Z)", re.MULTILINE | re.DOTALL)
_REVIEW_REQUEST_ID_RE = re.compile(r"^-\s*review_request_id:\s*(.+)$", re.MULTILINE)
_REVIEW_DECISION_ID_RE = re.compile(r"^-\s*review_decision_id:\s*(.+)$", re.MULTILINE)
_DECISION_RE = re.compile(r"^-\s*decision:\s*(.+)$", re.MULTILINE)
_STATUS_RE = re.compile(r"^-\s*status:\s*(.+)$", re.MULTILINE)
_SUBMITTED_AT_RE = re.compile(r"^-\s*submitted_at:\s*(.+)$", re.MULTILINE)
_SOURCE_RE = re.compile(r"^-\s*source:\s*(.+)$", re.MULTILINE)


def _publish_and_track_artifact(
    *,
    task_store: TaskStore,
    artifact_store: ArtifactStore,
    artifact_type: str,
    body: str,
    stage,
    producer: str,
    consumer: str,
    status: str,
) -> str:
    publication = artifact_store.publish_artifact(
        artifact_type=artifact_type,
        body=body,
        stage=stage,
        producer=producer,
        consumer=consumer,
        status=status,
    )
    task_store.set_current_artifact(artifact_type=artifact_type, artifact_path=publication.current_path)
    return publication.current_path


def _next_artifact_sequence_id(
    *,
    artifact_store: ArtifactStore,
    artifact_type: str,
    id_pattern: re.Pattern[str],
    default_current_id: str,
    prefix: str,
) -> str:
    current_path = artifact_store.current_artifact_path(artifact_type)
    if not current_path.exists():
        return f"{prefix}-0001"
    body = current_path.read_text(encoding="utf-8")
    current_id = _extract_first_match(id_pattern, body, default=default_current_id)
    try:
        next_index = int(current_id.rsplit("-", maxsplit=1)[-1]) + 1
    except ValueError:
        next_index = 2
    return f"{prefix}-{next_index:04d}"


@dataclass(frozen=True)
class ProgressSnapshot:
    latest_update: str
    current_focus: str
    next_step: str
    risks: str
    needs_human_review: bool
    user_summary: str


@dataclass(frozen=True)
class AdviceEntry:
    advice_id: str
    submitted_at: str
    status: str
    source: str
    body: str


@dataclass(frozen=True)
class ReviewRequest:
    review_request_id: str
    requested_at: str
    body: str


@dataclass(frozen=True)
class ReviewDecisionSubmissionResult:
    review_request_id: str
    review_decision_id: str
    decision: str
    current_stage: Stage
    current_owner: str
    status: str
    review_artifact_path: str


def ensure_supervision_artifacts(*, task_store: TaskStore, artifact_store: ArtifactStore) -> None:
    context = task_store.load_task_context()
    if constants.ARTIFACT_PROGRESS not in context.current_artifacts:
        _publish_and_track_artifact(
            task_store=task_store,
            artifact_store=artifact_store,
            artifact_type=constants.ARTIFACT_PROGRESS,
            body=_progress_body(
                latest_update="Gateway initialized.",
                current_focus=_default_current_focus(context),
                next_step=_default_next_step(context),
                risks="-",
                needs_human_review=False,
                user_summary="-",
                timeline_entries=(
                    _timeline_entry("system", "Gateway Initialized", "Task workspace initialized."),
                ),
            ),
            stage=context.current_stage,
            producer=constants.ROLE_ORCHESTRATOR,
            consumer="human",
            status="ready",
        )
    if constants.ARTIFACT_HUMAN_ADVICE_LOG not in context.current_artifacts:
        _publish_and_track_artifact(
            task_store=task_store,
            artifact_store=artifact_store,
            artifact_type=constants.ARTIFACT_HUMAN_ADVICE_LOG,
            body=_advice_log_body(()),
            stage=context.current_stage,
            producer="human",
            consumer=constants.ROLE_PRODUCT_OWNER,
            status="ready",
        )


def read_progress_snapshot(*, artifact_store: ArtifactStore) -> ProgressSnapshot:
    path = artifact_store.current_artifact_path(constants.ARTIFACT_PROGRESS)
    if not path.exists():
        return ProgressSnapshot(
            latest_update="-",
            current_focus="-",
            next_step="-",
            risks="-",
            needs_human_review=False,
            user_summary="-",
        )
    body = path.read_text(encoding="utf-8")
    return ProgressSnapshot(
        latest_update=_extract_summary_value(body, "latest_update", default="-"),
        current_focus=_extract_summary_value(body, "current_focus", default="-"),
        next_step=_extract_summary_value(body, "next_step", default="-"),
        risks=_extract_summary_value(body, "risks", default="-"),
        needs_human_review=_extract_summary_value(body, "needs_human_review", default="no") == "yes",
        user_summary=_extract_summary_value(body, "user_summary", default="-"),
    )


def publish_progress_update(
    *,
    task_store: TaskStore,
    artifact_store: ArtifactStore,
    latest_update: str,
    timeline_title: str,
    timeline_body: str,
    current_focus: str | None = None,
    next_step: str | None = None,
    risks: str | None = None,
    needs_human_review: bool | None = None,
    user_summary: str | None = None,
) -> str:
    ensure_supervision_artifacts(task_store=task_store, artifact_store=artifact_store)
    context = task_store.load_task_context()
    previous_path = artifact_store.current_artifact_path(constants.ARTIFACT_PROGRESS)
    previous_body = previous_path.read_text(encoding="utf-8") if previous_path.exists() else ""
    previous_timeline = _extract_timeline(previous_body)
    snapshot = read_progress_snapshot(artifact_store=artifact_store)
    resolved_focus = current_focus or _default_current_focus(context)
    resolved_next_step = next_step or _default_next_step(context)
    resolved_risks = risks if risks is not None else snapshot.risks
    if not resolved_risks:
        resolved_risks = "-"
    resolved_user_summary = user_summary if user_summary is not None else snapshot.user_summary
    if not resolved_user_summary:
        resolved_user_summary = "-"
    resolved_needs_review = (
        needs_human_review
        if needs_human_review is not None
        else context.status == TaskStatus.WAITING_APPROVAL
    )
    return _publish_and_track_artifact(
        task_store=task_store,
        artifact_store=artifact_store,
        artifact_type=constants.ARTIFACT_PROGRESS,
        body=_progress_body(
            latest_update=latest_update,
            current_focus=resolved_focus,
            next_step=resolved_next_step,
            risks=resolved_risks,
            needs_human_review=resolved_needs_review,
            user_summary=resolved_user_summary,
            timeline_entries=(*previous_timeline, _timeline_entry("system", timeline_title, timeline_body)),
        ),
        stage=context.current_stage,
        producer=constants.ROLE_ORCHESTRATOR,
        consumer="human",
        status="ready",
    )


def submit_human_advice(*, task_store: TaskStore, artifact_store: ArtifactStore, text: str, source: str = "status") -> str:
    context = task_store.load_task_context()
    if context.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.TERMINATED}:
        raise RuntimeError(f"task {context.task_id} is terminal ({context.status.value}); advice is not accepted")
    if context.status == TaskStatus.WAITING_APPROVAL:
        raise RuntimeError("task is waiting for human review; use `x_claw status --approve` or `--reject --comment ...`.")
    ensure_supervision_artifacts(task_store=task_store, artifact_store=artifact_store)
    entries = read_human_advice_entries(artifact_store=artifact_store)
    advice_id = f"advice-{len(entries) + 1:04d}"
    submitted_at = _utc_now_iso()
    next_entries = (*entries, AdviceEntry(advice_id=advice_id, submitted_at=submitted_at, status="pending", source=source, body=text.strip()))
    publication_path = _publish_and_track_artifact(
        task_store=task_store,
        artifact_store=artifact_store,
        artifact_type=constants.ARTIFACT_HUMAN_ADVICE_LOG,
        body=_advice_log_body(next_entries),
        stage=context.current_stage,
        producer="human",
        consumer=constants.ROLE_PRODUCT_OWNER,
        status="ready",
    )
    task_store.append_event(
        actor="human",
        action="human_advice_submitted",
        output_artifacts=(publication_path,),
        result="pending",
        notes=f"advice_id={advice_id}",
    )
    publish_progress_update(
        task_store=task_store,
        artifact_store=artifact_store,
        latest_update=f"Human advice submitted: {advice_id}",
        timeline_title="Human Advice Submitted",
        timeline_body=text.strip(),
        current_focus=_default_current_focus(context),
        next_step=_default_next_step(context),
    )
    return advice_id


def read_human_advice_entries(*, artifact_store: ArtifactStore) -> tuple[AdviceEntry, ...]:
    path = artifact_store.current_artifact_path(constants.ARTIFACT_HUMAN_ADVICE_LOG)
    if not path.exists():
        return ()
    body = path.read_text(encoding="utf-8")
    entries: list[AdviceEntry] = []
    for match in _ADVICE_SECTION_RE.finditer(body):
        section_body = match.group("body")
        advice_id = match.group("advice_id").strip()
        status = _extract_first_match(_STATUS_RE, section_body, default="pending")
        submitted_at = _extract_first_match(_SUBMITTED_AT_RE, section_body, default="-")
        source = _extract_first_match(_SOURCE_RE, section_body, default="status")
        content = _extract_body_after_heading(section_body, "Advice")
        entries.append(
            AdviceEntry(
                advice_id=advice_id,
                submitted_at=submitted_at,
                status=status,
                source=source,
                body=content,
            )
        )
    return tuple(entries)


def pending_human_advice_count(*, artifact_store: ArtifactStore) -> int:
    return sum(1 for entry in read_human_advice_entries(artifact_store=artifact_store) if entry.status == "pending")


def has_pending_human_advice(*, artifact_store: ArtifactStore) -> bool:
    return pending_human_advice_count(artifact_store=artifact_store) > 0


def resolve_pending_human_advice(
    *,
    task_store: TaskStore,
    artifact_store: ArtifactStore,
    disposition: HumanAdviceDisposition,
) -> int:
    entries = read_human_advice_entries(artifact_store=artifact_store)
    pending_count = 0
    next_entries: list[AdviceEntry] = []
    for entry in entries:
        if entry.status == "pending":
            pending_count += 1
            next_entries.append(
                AdviceEntry(
                    advice_id=entry.advice_id,
                    submitted_at=entry.submitted_at,
                    status=disposition.value,
                    source=entry.source,
                    body=entry.body,
                )
            )
        else:
            next_entries.append(entry)
    if pending_count == 0:
        return 0
    context = task_store.load_task_context()
    publication_path = _publish_and_track_artifact(
        task_store=task_store,
        artifact_store=artifact_store,
        artifact_type=constants.ARTIFACT_HUMAN_ADVICE_LOG,
        body=_advice_log_body(tuple(next_entries)),
        stage=context.current_stage,
        producer="human",
        consumer=constants.ROLE_PRODUCT_OWNER,
        status="ready",
    )
    task_store.append_event(
        actor=constants.ROLE_PRODUCT_OWNER,
        action="human_advice_disposition_recorded",
        output_artifacts=(publication_path,),
        result=disposition.value,
        notes=f"resolved={pending_count}",
    )
    return pending_count


def publish_review_request(
    *,
    task_store: TaskStore,
    artifact_store: ArtifactStore,
    summary: str,
    proposal_body: str,
    focus_items: tuple[str, ...] = (),
) -> str:
    context = task_store.load_task_context()
    review_request_id = _next_artifact_sequence_id(
        artifact_store=artifact_store,
        artifact_type=constants.ARTIFACT_REVIEW_REQUEST,
        id_pattern=_REVIEW_REQUEST_ID_RE,
        default_current_id="review-request-0000",
        prefix="review-request",
    )
    body = _review_request_body(
        review_request_id=review_request_id,
        requested_at=_utc_now_iso(),
        summary=summary,
        proposal_body=proposal_body,
        focus_items=focus_items,
    )
    publication_path = _publish_and_track_artifact(
        task_store=task_store,
        artifact_store=artifact_store,
        artifact_type=constants.ARTIFACT_REVIEW_REQUEST,
        body=body,
        stage=Stage.HUMAN_GATE,
        producer=constants.ROLE_PRODUCT_OWNER,
        consumer="human",
        status="ready",
    )
    task_store.append_event(
        actor=constants.ROLE_PRODUCT_OWNER,
        action="review_requested",
        output_artifacts=(publication_path,),
        result="waiting_approval",
        notes=f"review_request_id={review_request_id}",
    )
    publish_progress_update(
        task_store=task_store,
        artifact_store=artifact_store,
        latest_update=f"Human review requested: {review_request_id}",
        timeline_title="Human Review Requested",
        timeline_body=summary,
        current_focus="Waiting for human review on the current proposal.",
        next_step="Run `x_claw status --approve` or `x_claw status --reject --comment \"...\"`.",
        needs_human_review=True,
    )
    return review_request_id


def read_latest_review_request_id(*, artifact_store: ArtifactStore) -> str:
    path = artifact_store.current_artifact_path(constants.ARTIFACT_REVIEW_REQUEST)
    if not path.exists():
        return "-"
    body = path.read_text(encoding="utf-8")
    match = _REVIEW_REQUEST_ID_RE.search(body)
    if match is None:
        return "-"
    return match.group(1).strip() or "-"


def submit_review_decision(
    *,
    task_store: TaskStore,
    artifact_store: ArtifactStore,
    decision: str,
    comment: str,
) -> ReviewDecisionSubmissionResult:
    context = task_store.load_task_context()
    if context.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.TERMINATED}:
        raise RuntimeError(f"task {context.task_id} is terminal ({context.status.value}); review is not accepted")
    if context.current_stage != Stage.HUMAN_GATE or context.status != TaskStatus.WAITING_APPROVAL:
        raise RuntimeError("human review can only be submitted while task is waiting for approval.")
    normalized_decision = ReviewDecision(decision).value
    review_request_id = read_latest_review_request_id(artifact_store=artifact_store)
    if review_request_id == "-":
        raise RuntimeError("review request is missing; cannot submit review decision.")
    review_decision_id = _next_artifact_sequence_id(
        artifact_store=artifact_store,
        artifact_type=constants.ARTIFACT_REVIEW_DECISION,
        id_pattern=_REVIEW_DECISION_ID_RE,
        default_current_id="review-decision-0000",
        prefix="review-decision",
    )
    normalized_comment = comment.strip() or (
        "approved" if normalized_decision == ReviewDecision.APPROVED.value else "rejected"
    )
    publication_path = _publish_and_track_artifact(
        task_store=task_store,
        artifact_store=artifact_store,
        artifact_type=constants.ARTIFACT_REVIEW_DECISION,
        body=_review_decision_body(
            review_request_id=review_request_id,
            review_decision_id=review_decision_id,
            decision=normalized_decision,
            comment=normalized_comment,
        ),
        stage=Stage.HUMAN_GATE,
        producer="human",
        consumer=constants.ROLE_PRODUCT_OWNER,
        status="final",
    )
    next_stage = fixed_next_stage(Stage.HUMAN_GATE)
    assert next_stage is not None
    task_store.switch_stage_owner(
        stage=next_stage,
        owner=owner_for_stage(next_stage),
        status=TaskStatus.RUNNING,
    )
    task_store.append_event(
        actor="human",
        action="review_decision_submitted",
        output_artifacts=(publication_path,),
        result=normalized_decision,
        notes=f"review_request_id={review_request_id}; review_decision_id={review_decision_id}",
    )
    publish_progress_update(
        task_store=task_store,
        artifact_store=artifact_store,
        latest_update=f"Human review recorded: {normalized_decision}",
        timeline_title="Human Review Recorded",
        timeline_body=normalized_comment,
        current_focus="Product Owner is incorporating the latest human review.",
        next_step="Gateway continues automatically.",
        needs_human_review=False,
    )
    updated_context = task_store.load_task_context()
    return ReviewDecisionSubmissionResult(
        review_request_id=review_request_id,
        review_decision_id=review_decision_id,
        decision=normalized_decision,
        current_stage=updated_context.current_stage,
        current_owner=updated_context.current_owner,
        status=updated_context.status.value,
        review_artifact_path=publication_path,
    )


def _progress_body(
    *,
    latest_update: str,
    current_focus: str,
    next_step: str,
    risks: str,
    needs_human_review: bool,
    user_summary: str,
    timeline_entries: tuple[str, ...],
) -> str:
    return "\n".join(
        [
            "# Progress",
            "",
            "## Summary",
            "",
            f"- latest_update: {latest_update.strip()}",
            f"- current_focus: {current_focus.strip()}",
            f"- next_step: {next_step.strip()}",
            f"- risks: {risks.strip() or '-'}",
            f"- needs_human_review: {'yes' if needs_human_review else 'no'}",
            f"- user_summary: {user_summary.strip() or '-'}",
            "",
            "## Timeline",
            "",
            *timeline_entries,
            "",
        ]
    ).rstrip() + "\n"


def _timeline_entry(actor: str, title: str, body: str) -> str:
    return "\n".join(
        [
            f"### {_utc_now_iso()} [{actor}] {title}",
            "",
            body.strip() or "-",
        ]
    )


def _extract_timeline(body: str) -> tuple[str, ...]:
    if "## Timeline" not in body:
        return ()
    _, timeline = body.split("## Timeline", maxsplit=1)
    chunks = [chunk.strip() for chunk in timeline.strip().split("\n### ") if chunk.strip()]
    entries: list[str] = []
    for idx, chunk in enumerate(chunks):
        entry = chunk if idx == 0 and chunk.startswith("### ") else f"### {chunk}"
        entries.append(entry)
    return tuple(entries)


def _extract_summary_value(body: str, field: str, *, default: str) -> str:
    pattern = re.compile(_SUMMARY_FIELD_PATTERN.format(field=re.escape(field)), re.MULTILINE)
    match = pattern.search(body)
    if match is None:
        return default
    value = match.group(1).strip()
    return value or default


def _advice_log_body(entries: tuple[AdviceEntry, ...]) -> str:
    lines = [
        "# Human Advice Log",
        "",
        "## Advice Entries",
        "",
    ]
    if not entries:
        lines.append("No human advice has been submitted yet.")
        lines.append("")
        return "\n".join(lines)
    for entry in entries:
        lines.extend(
            [
                f"### {entry.advice_id}",
                "",
                f"- advice_id: {entry.advice_id}",
                f"- submitted_at: {entry.submitted_at}",
                f"- status: {entry.status}",
                f"- source: {entry.source}",
                "",
                "#### Advice",
                "",
                entry.body.strip(),
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _extract_body_after_heading(section_body: str, heading: str) -> str:
    marker = f"#### {heading}"
    if marker not in section_body:
        return section_body.strip()
    _, suffix = section_body.split(marker, maxsplit=1)
    return suffix.strip() or "-"


def _extract_first_match(pattern: re.Pattern[str], text: str, *, default: str) -> str:
    match = pattern.search(text)
    if match is None:
        return default
    value = match.group(1).strip()
    return value or default


def _review_request_body(
    *,
    review_request_id: str,
    requested_at: str,
    summary: str,
    proposal_body: str,
    focus_items: tuple[str, ...],
) -> str:
    focus_lines = [f"- {item}" for item in focus_items] if focus_items else ["- Validate whether the current proposal is acceptable."]
    return "\n".join(
        [
            "# Review Request",
            "",
            "## Request Meta",
            "",
            f"- review_request_id: {review_request_id}",
            f"- requested_at: {requested_at}",
            f"- requested_by: {constants.ROLE_PRODUCT_OWNER}",
            f"- source_stage: {Stage.PRODUCT_OWNER_DISPATCH.value}",
            "",
            "## Request Summary",
            "",
            summary.strip(),
            "",
            "## Current Proposal",
            "",
            proposal_body.strip() or "-",
            "",
            "## Suggested Focus",
            "",
            *focus_lines,
            "",
        ]
    )


def _review_decision_body(*, review_request_id: str, review_decision_id: str, decision: str, comment: str) -> str:
    return "\n".join(
        [
            "# Review Decision",
            "",
            "## Decision Meta",
            "",
            f"- review_request_id: {review_request_id}",
            f"- review_decision_id: {review_decision_id}",
            f"- decision: {decision}",
            f"- submitted_at: {_utc_now_iso()}",
            f"- source: status",
            "",
            "## Comment",
            "",
            comment.strip() or "-",
            "",
        ]
    )


def _default_current_focus(context) -> str:
    if context.status == TaskStatus.WAITING_APPROVAL:
        return "Waiting for human review on the current proposal."
    if context.status == TaskStatus.COMPLETED:
        return "Task completed."
    if context.status == TaskStatus.FAILED:
        return "Task failed."
    if context.status == TaskStatus.TERMINATED:
        return "Task terminated."
    return f"Current stage: {context.current_stage.value}."


def _default_next_step(context) -> str:
    if context.status == TaskStatus.WAITING_APPROVAL:
        return "Run `x_claw status --approve` or `x_claw status --reject --comment \"...\"`."
    if context.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.TERMINATED}:
        return "No further automatic steps."
    return "Gateway continues automatically."


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


__all__ = [
    "AdviceEntry",
    "ProgressSnapshot",
    "ReviewDecisionSubmissionResult",
    "ReviewRequest",
    "ensure_supervision_artifacts",
    "has_pending_human_advice",
    "pending_human_advice_count",
    "publish_progress_update",
    "publish_review_request",
    "read_human_advice_entries",
    "read_latest_review_request_id",
    "read_progress_snapshot",
    "resolve_pending_human_advice",
    "submit_human_advice",
    "submit_review_decision",
]
