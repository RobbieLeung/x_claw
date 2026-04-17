"""Domain data models shared by gateway runtime, stores, and adapters."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Mapping, TypeVar

from . import protocol as constants
from .protocol import AgentExecutionStatus, AgentResultType, Stage, TaskStatus

ModelType = TypeVar("ModelType", bound="SerializableModel")

_HUMAN_ACTOR = "human"
_SYSTEM_ACTOR = "system"


def _require_non_empty_string(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string.")
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field_name} must be non-empty.")
    return stripped


def _coerce_bool(value: bool, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{field_name} must be a bool.")
    return value


def _coerce_enum(value: Any, enum_type: type[Enum], field_name: str) -> Enum:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except ValueError as exc:
        allowed = ", ".join(member.value for member in enum_type)
        raise ValueError(f"{field_name} must be one of: {allowed}.") from exc


def _coerce_role(
    value: str,
    field_name: str,
    *,
    allow_human: bool = False,
    allow_system: bool = False,
) -> str:
    normalized = _require_non_empty_string(value, field_name)
    allowed_roles = set(constants.ROLE_NAMES)
    if allow_human:
        allowed_roles.add(_HUMAN_ACTOR)
    if allow_system:
        allowed_roles.add(_SYSTEM_ACTOR)
    if normalized not in allowed_roles:
        allowed_display = ", ".join(sorted(allowed_roles))
        raise ValueError(f"{field_name} must be one of: {allowed_display}.")
    return normalized


def _coerce_tuple_of_strings(
    values: tuple[str, ...] | list[str],
    field_name: str,
) -> tuple[str, ...]:
    if isinstance(values, tuple):
        iterable = values
    elif isinstance(values, list):
        iterable = tuple(values)
    else:
        raise TypeError(f"{field_name} must be a tuple[str, ...] or list[str].")
    return tuple(_require_non_empty_string(value, field_name) for value in iterable)


def _coerce_artifact_map(values: Mapping[str, str], field_name: str) -> dict[str, str]:
    if not isinstance(values, Mapping):
        raise TypeError(f"{field_name} must be a mapping.")
    normalized: dict[str, str] = {}
    for artifact_type, artifact_path in values.items():
        artifact_type_name = _require_non_empty_string(str(artifact_type), field_name)
        if artifact_type_name not in constants.ARTIFACT_TYPES:
            allowed = ", ".join(constants.ARTIFACT_TYPES)
            raise ValueError(
                f"{field_name} key {artifact_type_name!r} is invalid. Allowed: {allowed}.",
            )
        normalized[artifact_type_name] = _require_non_empty_string(str(artifact_path), field_name)
    return normalized


def _serialize_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _serialize_value(nested) for key, nested in value.items()}
    if isinstance(value, list):
        return [_serialize_value(nested) for nested in value]
    if isinstance(value, tuple):
        return [_serialize_value(nested) for nested in value]
    return value


@dataclass
class SerializableModel:
    """Base model with stable dict serialization helpers."""

    def to_dict(self) -> dict[str, Any]:
        return _serialize_value(asdict(self))

    @classmethod
    def from_dict(cls: type[ModelType], payload: Mapping[str, Any]) -> ModelType:
        if not isinstance(payload, Mapping):
            raise TypeError(f"{cls.__name__}.from_dict expects a mapping.")
        return cls(**dict(payload))


@dataclass
class RepositoryScopedRecord(SerializableModel):
    """Shared target-repo and workspace metadata."""

    target_repo_path: str
    task_workspace_path: str | None = None
    target_repo_git_root: str | None = None
    target_repo_head: str | None = None
    target_repo_dirty: bool | None = None

    def __post_init__(self) -> None:
        self.target_repo_path = _require_non_empty_string(
            self.target_repo_path,
            constants.FIELD_TARGET_REPO_PATH,
        )
        if self.task_workspace_path is not None:
            self.task_workspace_path = _require_non_empty_string(
                self.task_workspace_path,
                constants.FIELD_TASK_WORKSPACE_PATH,
            )
        if self.target_repo_git_root is not None:
            self.target_repo_git_root = _require_non_empty_string(
                self.target_repo_git_root,
                constants.FIELD_TARGET_REPO_GIT_ROOT,
            )
        if self.target_repo_head is not None:
            self.target_repo_head = _require_non_empty_string(
                self.target_repo_head,
                constants.FIELD_TARGET_REPO_HEAD,
            )
        if self.target_repo_dirty is not None:
            self.target_repo_dirty = _coerce_bool(
                self.target_repo_dirty,
                constants.FIELD_TARGET_REPO_DIRTY,
            )


@dataclass
class ArtifactBackedRecord(RepositoryScopedRecord):
    """Reusable artifact locator metadata used by store and adapter logic."""

    artifact_path: str = ""
    version: int = 1
    supersedes: str | None = None

    def __post_init__(self) -> None:
        RepositoryScopedRecord.__post_init__(self)
        self.artifact_path = _require_non_empty_string(self.artifact_path, "artifact_path")
        if not isinstance(self.version, int):
            raise TypeError("version must be an int.")
        if self.version < 1:
            raise ValueError("version must be >= 1.")
        if self.supersedes is not None:
            self.supersedes = _require_non_empty_string(self.supersedes, "supersedes")


@dataclass
class TaskEvent(SerializableModel):
    """One append-only event row for `event_log.md`."""

    seq: int
    timestamp: str
    actor: str
    action: str
    input_artifacts: tuple[str, ...] | list[str] = field(default_factory=tuple)
    output_artifacts: tuple[str, ...] | list[str] = field(default_factory=tuple)
    result: str = "ok"
    notes: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.seq, int):
            raise TypeError("seq must be an int.")
        if self.seq < 1:
            raise ValueError("seq must be >= 1.")
        self.timestamp = _require_non_empty_string(self.timestamp, "timestamp")
        self.actor = _coerce_role(
            self.actor,
            "actor",
            allow_human=True,
            allow_system=True,
        )
        self.action = _require_non_empty_string(self.action, "action")
        self.input_artifacts = _coerce_tuple_of_strings(self.input_artifacts, "input_artifacts")
        self.output_artifacts = _coerce_tuple_of_strings(self.output_artifacts, "output_artifacts")
        self.result = _require_non_empty_string(self.result, "result")
        if self.notes is not None:
            self.notes = _require_non_empty_string(self.notes, "notes")


@dataclass
class TaskContext(SerializableModel):
    """Global orchestration context required for routing and recovery."""

    task_id: str
    task_workspace_path: str
    target_repo_path: str
    bootstrap_plan_source_path: str | None = None
    target_repo_git_root: str | None = None
    target_repo_head: str | None = None
    target_repo_dirty: bool = False
    current_stage: Stage | str = Stage.INTAKE
    status: TaskStatus | str = TaskStatus.RUNNING
    current_owner: str = constants.ROLE_ORCHESTRATOR
    current_artifacts: dict[str, str] = field(default_factory=dict)
    latest_event_seq: int = 0
    repair_loop_count: int = 0
    recovery_notes: tuple[str, ...] | list[str] = field(default_factory=tuple)
    gateway_pid: int | None = None

    def __post_init__(self) -> None:
        self.task_id = _require_non_empty_string(self.task_id, constants.FIELD_TASK_ID)
        self.task_workspace_path = _require_non_empty_string(
            self.task_workspace_path,
            constants.FIELD_TASK_WORKSPACE_PATH,
        )
        if self.bootstrap_plan_source_path is not None:
            self.bootstrap_plan_source_path = _require_non_empty_string(
                self.bootstrap_plan_source_path,
                constants.FIELD_BOOTSTRAP_PLAN_SOURCE_PATH,
            )
        self.target_repo_path = _require_non_empty_string(
            self.target_repo_path,
            constants.FIELD_TARGET_REPO_PATH,
        )
        if self.target_repo_git_root is not None:
            self.target_repo_git_root = _require_non_empty_string(
                self.target_repo_git_root,
                constants.FIELD_TARGET_REPO_GIT_ROOT,
            )
        if self.target_repo_head is not None:
            self.target_repo_head = _require_non_empty_string(
                self.target_repo_head,
                constants.FIELD_TARGET_REPO_HEAD,
            )
        self.target_repo_dirty = _coerce_bool(self.target_repo_dirty, constants.FIELD_TARGET_REPO_DIRTY)
        self.current_stage = _coerce_enum(self.current_stage, Stage, "current_stage")  # type: ignore[assignment]
        self.status = _coerce_enum(self.status, TaskStatus, "status")  # type: ignore[assignment]
        self.current_owner = _coerce_role(
            self.current_owner,
            "current_owner",
            allow_human=True,
            allow_system=True,
        )
        self.current_artifacts = _coerce_artifact_map(self.current_artifacts, "current_artifacts")
        if not isinstance(self.latest_event_seq, int):
            raise TypeError("latest_event_seq must be an int.")
        if self.latest_event_seq < 0:
            raise ValueError("latest_event_seq must be >= 0.")
        if not isinstance(self.repair_loop_count, int):
            raise TypeError("repair_loop_count must be an int.")
        if self.repair_loop_count < 0:
            raise ValueError("repair_loop_count must be >= 0.")
        self.recovery_notes = _coerce_tuple_of_strings(self.recovery_notes, "recovery_notes")
        if self.gateway_pid is not None:
            if not isinstance(self.gateway_pid, int):
                raise TypeError("gateway_pid must be an int or None.")
            if self.gateway_pid <= 0:
                raise ValueError("gateway_pid must be > 0 when provided.")

    def is_running(self) -> bool:
        return self.status == TaskStatus.RUNNING

    def is_waiting_approval(self) -> bool:
        return self.status == TaskStatus.WAITING_APPROVAL


@dataclass
class AgentResult(ArtifactBackedRecord):
    """Role execution output with explicit semantic result type."""

    result_id: str = ""
    role: str = constants.ROLE_PRODUCT_OWNER
    result_type: AgentResultType | str = AgentResultType.ROUTE_DECISION_RESULT
    execution_status: AgentExecutionStatus | str = AgentExecutionStatus.SUCCEEDED
    summary: str = ""
    next_actions: tuple[str, ...] | list[str] = field(default_factory=tuple)
    warnings: tuple[str, ...] | list[str] = field(default_factory=tuple)
    run_directory: str | None = None

    def __post_init__(self) -> None:
        ArtifactBackedRecord.__post_init__(self)
        self.result_id = _require_non_empty_string(self.result_id, "result_id")
        self.role = _coerce_role(self.role, "role")
        self.result_type = _coerce_enum(self.result_type, AgentResultType, "result_type")  # type: ignore[assignment]
        self.execution_status = _coerce_enum(
            self.execution_status,
            AgentExecutionStatus,
            "execution_status",
        )  # type: ignore[assignment]
        self.summary = _require_non_empty_string(self.summary, "summary")
        self.next_actions = _coerce_tuple_of_strings(self.next_actions, "next_actions")
        self.warnings = _coerce_tuple_of_strings(self.warnings, "warnings")
        if self.run_directory is not None:
            self.run_directory = _require_non_empty_string(self.run_directory, "run_directory")


__all__ = [
    "AgentResult",
    "ArtifactBackedRecord",
    "RepositoryScopedRecord",
    "SerializableModel",
    "TaskContext",
    "TaskEvent",
]
