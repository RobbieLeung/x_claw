"""Canonical protocol definitions for xclaw runtime and persisted contracts."""

from __future__ import annotations

from enum import Enum
from types import MappingProxyType
from typing import Final

CONFIG_KEY_WORKSPACE_ROOT: Final = "workspace_root"
CONFIG_KEY_MAX_REPAIR_LOOPS: Final = "max_repair_loops"

DEFAULT_WORKSPACE_ROOT: Final = "workspace"
DEFAULT_MAX_REPAIR_LOOPS: Final = 2

TASK_STATUS_RUNNING: Final = "running"
TASK_STATUS_WAITING_APPROVAL: Final = "waiting_approval"
TASK_STATUS_COMPLETED: Final = "completed"
TASK_STATUS_FAILED: Final = "failed"
TASK_STATUS_TERMINATED: Final = "terminated"

TASK_STATUS_NAMES: Final[tuple[str, ...]] = (
    TASK_STATUS_RUNNING,
    TASK_STATUS_WAITING_APPROVAL,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_TERMINATED,
)


class TaskStatus(str, Enum):
    """Lifecycle of one orchestrated task."""

    RUNNING = TASK_STATUS_RUNNING
    WAITING_APPROVAL = TASK_STATUS_WAITING_APPROVAL
    COMPLETED = TASK_STATUS_COMPLETED
    FAILED = TASK_STATUS_FAILED
    TERMINATED = TASK_STATUS_TERMINATED


DEFAULT_SETTINGS: Final[MappingProxyType[str, object]] = MappingProxyType(
    {
        CONFIG_KEY_WORKSPACE_ROOT: DEFAULT_WORKSPACE_ROOT,
        CONFIG_KEY_MAX_REPAIR_LOOPS: DEFAULT_MAX_REPAIR_LOOPS,
    }
)

TASK_FILENAME: Final = "task.md"
EVENT_LOG_FILENAME: Final = "event_log.md"
CURRENT_DIRNAME: Final = "current"
HISTORY_DIRNAME: Final = "history"
RUNS_DIRNAME: Final = "runs"

WORKSPACE_REQUIRED_DIRS: Final[tuple[str, ...]] = (
    CURRENT_DIRNAME,
    HISTORY_DIRNAME,
    RUNS_DIRNAME,
)
WORKSPACE_REQUIRED_FILES: Final[tuple[str, ...]] = (
    TASK_FILENAME,
    EVENT_LOG_FILENAME,
)

ROLE_PRODUCT_OWNER: Final = "product_owner"
ROLE_PROJECT_MANAGER: Final = "project_manager"
ROLE_DEVELOPER: Final = "developer"
ROLE_TESTER: Final = "tester"
ROLE_QA: Final = "qa"
ROLE_HUMAN_GATE: Final = "human_gate"
ROLE_ORCHESTRATOR: Final = "orchestrator"

ROLE_NAMES: Final[tuple[str, ...]] = (
    ROLE_PRODUCT_OWNER,
    ROLE_PROJECT_MANAGER,
    ROLE_DEVELOPER,
    ROLE_TESTER,
    ROLE_QA,
    ROLE_HUMAN_GATE,
    ROLE_ORCHESTRATOR,
)

ARTIFACT_REQUIREMENT_SPEC: Final = "requirement_spec"
ARTIFACT_EXECUTION_PLAN: Final = "execution_plan"
ARTIFACT_RESEARCH_BRIEF: Final = "research_brief"
ARTIFACT_DEV_HANDOFF: Final = "dev_handoff"
ARTIFACT_TEST_HANDOFF: Final = "test_handoff"
ARTIFACT_IMPLEMENTATION_RESULT: Final = "implementation_result"
ARTIFACT_TEST_REPORT: Final = "test_report"
ARTIFACT_QA_RESULT: Final = "qa_result"
ARTIFACT_REPAIR_TICKET: Final = "repair_ticket"
ARTIFACT_ROUTE_DECISION: Final = "route_decision"
ARTIFACT_CLOSEOUT: Final = "closeout"
ARTIFACT_PROGRESS: Final = "progress"
ARTIFACT_HUMAN_ADVICE_LOG: Final = "human_advice_log"
ARTIFACT_REVIEW_REQUEST: Final = "review_request"
ARTIFACT_REVIEW_DECISION: Final = "review_decision"

ARTIFACT_TYPES: Final[tuple[str, ...]] = (
    ARTIFACT_REQUIREMENT_SPEC,
    ARTIFACT_EXECUTION_PLAN,
    ARTIFACT_RESEARCH_BRIEF,
    ARTIFACT_DEV_HANDOFF,
    ARTIFACT_TEST_HANDOFF,
    ARTIFACT_IMPLEMENTATION_RESULT,
    ARTIFACT_TEST_REPORT,
    ARTIFACT_QA_RESULT,
    ARTIFACT_REPAIR_TICKET,
    ARTIFACT_ROUTE_DECISION,
    ARTIFACT_CLOSEOUT,
    ARTIFACT_PROGRESS,
    ARTIFACT_HUMAN_ADVICE_LOG,
    ARTIFACT_REVIEW_REQUEST,
    ARTIFACT_REVIEW_DECISION,
)

ARTIFACT_FILENAMES: Final[MappingProxyType[str, str]] = MappingProxyType(
    {artifact_type: f"{artifact_type}.md" for artifact_type in ARTIFACT_TYPES}
)

HUMAN_ADVICE_DISPOSITION_ACCEPTED: Final = "accepted"
HUMAN_ADVICE_DISPOSITION_PARTIALLY_ACCEPTED: Final = "partially_accepted"
HUMAN_ADVICE_DISPOSITION_REJECTED: Final = "rejected"
HUMAN_ADVICE_DISPOSITION_NONE: Final = "none"

HUMAN_ADVICE_DISPOSITION_NAMES: Final[tuple[str, ...]] = (
    HUMAN_ADVICE_DISPOSITION_ACCEPTED,
    HUMAN_ADVICE_DISPOSITION_PARTIALLY_ACCEPTED,
    HUMAN_ADVICE_DISPOSITION_REJECTED,
    HUMAN_ADVICE_DISPOSITION_NONE,
)


class HumanAdviceDisposition(str, Enum):
    """How Product Owner handled the latest formal human advice batch."""

    ACCEPTED = HUMAN_ADVICE_DISPOSITION_ACCEPTED
    PARTIALLY_ACCEPTED = HUMAN_ADVICE_DISPOSITION_PARTIALLY_ACCEPTED
    REJECTED = HUMAN_ADVICE_DISPOSITION_REJECTED
    NONE = HUMAN_ADVICE_DISPOSITION_NONE


REVIEW_DECISION_APPROVED: Final = "approved"
REVIEW_DECISION_REJECTED: Final = "rejected"
REVIEW_DECISION_NAMES: Final[tuple[str, ...]] = (
    REVIEW_DECISION_APPROVED,
    REVIEW_DECISION_REJECTED,
)


class ReviewDecision(str, Enum):
    APPROVED = REVIEW_DECISION_APPROVED
    REJECTED = REVIEW_DECISION_REJECTED


class AgentResultType(str, Enum):
    RESEARCH_RESULT = "research_result"
    IMPLEMENTATION_RESULT = "implementation_result"
    TEST_RESULT = "test_result"
    QA_RESULT = "qa_result"
    ROUTE_DECISION_RESULT = "route_decision_result"


class AgentExecutionStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


FIELD_TASK_ID: Final = "task_id"
FIELD_TASK_WORKSPACE_PATH: Final = "task_workspace_path"
FIELD_TARGET_REPO_PATH: Final = "target_repo_path"
FIELD_TARGET_REPO_GIT_ROOT: Final = "target_repo_git_root"
FIELD_TARGET_REPO_HEAD: Final = "target_repo_head"
FIELD_TARGET_REPO_DIRTY: Final = "target_repo_dirty"

TARGET_REPO_CONTEXT_FIELDS: Final[tuple[str, ...]] = (
    FIELD_TARGET_REPO_PATH,
    FIELD_TARGET_REPO_GIT_ROOT,
    FIELD_TARGET_REPO_HEAD,
    FIELD_TARGET_REPO_DIRTY,
)

TASK_CONTEXT_FIELDS: Final[tuple[str, ...]] = (
    FIELD_TASK_ID,
    FIELD_TASK_WORKSPACE_PATH,
    *TARGET_REPO_CONTEXT_FIELDS,
)


class StageTransitionError(ValueError):
    """Raised when a stage transition is invalid for the selected path."""


class Stage(str, Enum):
    INTAKE = "intake"
    PRODUCT_OWNER_REFINEMENT = "product_owner_refinement"
    PROJECT_MANAGER_RESEARCH = "project_manager_research"
    PRODUCT_OWNER_DISPATCH = "product_owner_dispatch"
    DEVELOPER = "developer"
    TESTER = "tester"
    QA = "qa"
    HUMAN_GATE = "human_gate"
    CLOSEOUT = "closeout"


STAGE_DISPLAY_NAMES: Final[MappingProxyType[Stage, str]] = MappingProxyType(
    {
        Stage.INTAKE: "Intake",
        Stage.PRODUCT_OWNER_REFINEMENT: "Product Owner (Requirement Refinement)",
        Stage.PROJECT_MANAGER_RESEARCH: "Project Manager",
        Stage.PRODUCT_OWNER_DISPATCH: "Product Owner (Finalize and Dispatch)",
        Stage.DEVELOPER: "Developer",
        Stage.TESTER: "Tester",
        Stage.QA: "QA",
        Stage.HUMAN_GATE: "Human Gate",
        Stage.CLOSEOUT: "Closeout",
    }
)

STAGE_OWNERS: Final[MappingProxyType[Stage, str]] = MappingProxyType(
    {
        Stage.INTAKE: ROLE_ORCHESTRATOR,
        Stage.PRODUCT_OWNER_REFINEMENT: ROLE_PRODUCT_OWNER,
        Stage.PROJECT_MANAGER_RESEARCH: ROLE_PROJECT_MANAGER,
        Stage.PRODUCT_OWNER_DISPATCH: ROLE_PRODUCT_OWNER,
        Stage.DEVELOPER: ROLE_DEVELOPER,
        Stage.TESTER: ROLE_TESTER,
        Stage.QA: ROLE_QA,
        Stage.HUMAN_GATE: ROLE_HUMAN_GATE,
        Stage.CLOSEOUT: ROLE_ORCHESTRATOR,
    }
)

PRODUCT_OWNER_ROUTE_STAGES: Final[tuple[Stage, ...]] = (
    Stage.PRODUCT_OWNER_REFINEMENT,
    Stage.PRODUCT_OWNER_DISPATCH,
)

PRODUCT_OWNER_ROUTE_TARGETS: Final[MappingProxyType[Stage, tuple[Stage, ...]]] = MappingProxyType(
    {
        Stage.PRODUCT_OWNER_REFINEMENT: (
            Stage.PROJECT_MANAGER_RESEARCH,
            Stage.PRODUCT_OWNER_DISPATCH,
        ),
        Stage.PRODUCT_OWNER_DISPATCH: (
            Stage.PROJECT_MANAGER_RESEARCH,
            Stage.DEVELOPER,
            Stage.TESTER,
            Stage.QA,
            Stage.HUMAN_GATE,
            Stage.CLOSEOUT,
        ),
    }
)


def owner_for_stage(stage: Stage | str) -> str:
    return STAGE_OWNERS[Stage(stage)]


def supports_product_owner_advice(stage: Stage | str) -> bool:
    return Stage(stage) in PRODUCT_OWNER_ROUTE_STAGES


def fixed_next_stage(stage: Stage | str) -> Stage | None:
    normalized = Stage(stage)
    if normalized == Stage.INTAKE:
        return Stage.PRODUCT_OWNER_REFINEMENT
    if normalized == Stage.PRODUCT_OWNER_REFINEMENT:
        raise StageTransitionError(
            "product_owner_refinement requires explicit Product Owner routing decision.",
        )
    if normalized == Stage.PROJECT_MANAGER_RESEARCH:
        return Stage.PRODUCT_OWNER_DISPATCH
    if normalized == Stage.PRODUCT_OWNER_DISPATCH:
        raise StageTransitionError(
            "product_owner_dispatch requires explicit Product Owner routing decision.",
        )
    if normalized == Stage.DEVELOPER:
        return Stage.PRODUCT_OWNER_DISPATCH
    if normalized == Stage.TESTER:
        return Stage.PRODUCT_OWNER_DISPATCH
    if normalized == Stage.QA:
        return Stage.PRODUCT_OWNER_DISPATCH
    if normalized == Stage.HUMAN_GATE:
        return Stage.PRODUCT_OWNER_DISPATCH
    if normalized == Stage.CLOSEOUT:
        return None
    raise StageTransitionError(f"unsupported stage transition: {normalized.value}")


def route_targets_for_stage(stage: Stage | str) -> tuple[Stage, ...]:
    normalized = Stage(stage)
    if normalized not in PRODUCT_OWNER_ROUTE_TARGETS:
        raise StageTransitionError(
            f"{normalized.value} does not accept Product Owner route decisions.",
        )
    return PRODUCT_OWNER_ROUTE_TARGETS[normalized]


__all__ = [
    "AgentExecutionStatus",
    "AgentResultType",
    "ARTIFACT_CLOSEOUT",
    "ARTIFACT_DEV_HANDOFF",
    "ARTIFACT_EXECUTION_PLAN",
    "ARTIFACT_FILENAMES",
    "ARTIFACT_HUMAN_ADVICE_LOG",
    "ARTIFACT_IMPLEMENTATION_RESULT",
    "ARTIFACT_PROGRESS",
    "ARTIFACT_QA_RESULT",
    "ARTIFACT_REPAIR_TICKET",
    "ARTIFACT_REQUIREMENT_SPEC",
    "ARTIFACT_RESEARCH_BRIEF",
    "ARTIFACT_REVIEW_DECISION",
    "ARTIFACT_REVIEW_REQUEST",
    "ARTIFACT_ROUTE_DECISION",
    "ARTIFACT_TEST_HANDOFF",
    "ARTIFACT_TEST_REPORT",
    "ARTIFACT_TYPES",
    "CONFIG_KEY_MAX_REPAIR_LOOPS",
    "CONFIG_KEY_WORKSPACE_ROOT",
    "DEFAULT_MAX_REPAIR_LOOPS",
    "DEFAULT_SETTINGS",
    "DEFAULT_WORKSPACE_ROOT",
    "EVENT_LOG_FILENAME",
    "FIELD_TARGET_REPO_DIRTY",
    "FIELD_TARGET_REPO_GIT_ROOT",
    "FIELD_TARGET_REPO_HEAD",
    "FIELD_TARGET_REPO_PATH",
    "FIELD_TASK_ID",
    "FIELD_TASK_WORKSPACE_PATH",
    "HISTORY_DIRNAME",
    "HUMAN_ADVICE_DISPOSITION_ACCEPTED",
    "HUMAN_ADVICE_DISPOSITION_NAMES",
    "HUMAN_ADVICE_DISPOSITION_NONE",
    "HUMAN_ADVICE_DISPOSITION_PARTIALLY_ACCEPTED",
    "HUMAN_ADVICE_DISPOSITION_REJECTED",
    "HumanAdviceDisposition",
    "CURRENT_DIRNAME",
    "ROLE_DEVELOPER",
    "ROLE_HUMAN_GATE",
    "ROLE_NAMES",
    "ROLE_ORCHESTRATOR",
    "ROLE_PRODUCT_OWNER",
    "ROLE_PROJECT_MANAGER",
    "ROLE_QA",
    "ROLE_TESTER",
    "RUNS_DIRNAME",
    "PRODUCT_OWNER_ROUTE_STAGES",
    "REVIEW_DECISION_APPROVED",
    "REVIEW_DECISION_NAMES",
    "REVIEW_DECISION_REJECTED",
    "ReviewDecision",
    "STAGE_DISPLAY_NAMES",
    "STAGE_OWNERS",
    "Stage",
    "StageTransitionError",
    "supports_product_owner_advice",
    "route_targets_for_stage",
    "owner_for_stage",
    "fixed_next_stage",
    "TARGET_REPO_CONTEXT_FIELDS",
    "TASK_CONTEXT_FIELDS",
    "TASK_FILENAME",
    "TASK_STATUS_COMPLETED",
    "TASK_STATUS_FAILED",
    "TASK_STATUS_NAMES",
    "TASK_STATUS_RUNNING",
    "TASK_STATUS_TERMINATED",
    "TASK_STATUS_WAITING_APPROVAL",
    "TaskStatus",
    "WORKSPACE_REQUIRED_DIRS",
    "WORKSPACE_REQUIRED_FILES",
]
