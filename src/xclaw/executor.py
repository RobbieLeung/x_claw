"""Stage-driven executor for the xclaw gateway."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re

from . import protocol as constants
from .agent_adapter import AgentAdapter, AgentInvocation, AgentInvocationResult
from .artifact_store import ArtifactPublication, ArtifactStore
from .models import (
    TaskContext,
    TaskEvent,
)
from .protocol import (
    AgentExecutionStatus,
    HumanAdviceDisposition,
    ReviewKind,
    Stage,
    TaskStatus,
    fixed_next_stage,
    owner_for_stage,
    route_targets_for_stage,
)
from .task_store import TaskStore
from .human_io import (
    has_pending_human_advice,
    publish_progress_update,
    publish_review_request,
    read_current_review_decision,
    read_progress_snapshot,
    resolve_pending_human_advice,
)

_BULLET_FIELD_TEMPLATE = r"^-\s*{field_name}:\s*(.+)$"
_TABLE_FIELD_TEMPLATE = r"^\|\s*{field_name}\s*\|\s*([^\|\n]+?)\s*\|$"
@dataclass(frozen=True)
class StageOutcome:
    """Stable result payload for one stage advancement."""

    task_id: str
    stage_executed: Stage
    next_stage: Stage | None
    task_status: TaskStatus
    current_owner: str
    published_artifacts: tuple[str, ...]
    message: str


@dataclass(frozen=True)
class RouteDecision:
    """Parsed Product Owner route decision contract."""

    next_stage: Stage | None
    task_status: TaskStatus
    based_on_artifacts: tuple[str, ...]
    human_advice_disposition: HumanAdviceDisposition
    review_kind_requested: ReviewKind | None


@dataclass(frozen=True)
class RepairLoopIncrement:
    """Validated repair-loop increment information."""

    loop_count: int
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class PlanSnapshot:
    """Minimal parsed plan metadata used by routing constraints."""

    plan_revision: str
    human_confirmation_required: bool
    human_confirmation_items: tuple[str, ...]
    active_subtask_id: str | None


@dataclass(frozen=True)
class _ArtifactPlan:
    artifact_type: str
    producer: str
    consumer: str
    status: str
    title: str


class StageExecutor:
    """Execute one stage transition while keeping task.md authoritative."""

    def __init__(
        self,
        task_workspace_path: str | Path,
        *,
        task_store: TaskStore | None = None,
        artifact_store: ArtifactStore | None = None,
        agent_adapter: AgentAdapter | None = None,
        agents_dir: str | Path | None = None,
        skills_dir: str | Path | None = None,
        max_repair_loops: int = constants.DEFAULT_MAX_REPAIR_LOOPS,
    ) -> None:
        self.task_workspace_path = Path(task_workspace_path).expanduser().resolve()
        self.task_store = task_store or TaskStore(self.task_workspace_path)
        self.artifact_store = artifact_store or ArtifactStore(self.task_workspace_path)
        self.agent_adapter = agent_adapter or AgentAdapter(
            self.task_workspace_path,
            agents_dir=agents_dir,
            skills_dir=skills_dir,
        )
        self.max_repair_loops = int(max_repair_loops)
        if self.max_repair_loops < 0:
            raise ValueError("max_repair_loops must be >= 0.")

    def advance(self) -> StageOutcome:
        """Advance the current running stage by one step."""

        context = self.task_store.load_task_context()
        if context.status != TaskStatus.RUNNING:
            raise RuntimeError(
                f"stage advancement requires running task status; got {context.status.value}",
            )

        try:
            if context.current_stage == Stage.INTAKE:
                return self._advance_intake()
            if context.current_stage == Stage.PRODUCT_OWNER_REFINEMENT:
                return self._execute_product_owner_stage(stage=Stage.PRODUCT_OWNER_REFINEMENT)
            if context.current_stage == Stage.ARCHITECT_PLANNING:
                return self._execute_architect_planning()
            if context.current_stage == Stage.PRODUCT_OWNER_DISPATCH:
                return self._execute_product_owner_stage(stage=Stage.PRODUCT_OWNER_DISPATCH)
            if context.current_stage == Stage.DEVELOPER:
                return self._execute_developer()
            if context.current_stage == Stage.TESTER:
                return self._execute_tester()
            if context.current_stage == Stage.HUMAN_GATE:
                raise RuntimeError(
                    "human_gate must wait for formal approval and cannot advance while running",
                )
            if context.current_stage == Stage.CLOSEOUT:
                return self._publish_closeout_and_complete(stage=context.current_stage)
            raise RuntimeError(f"unsupported current stage: {context.current_stage.value}")
        except RuntimeError as exc:
            updated = self.task_store.load_task_context()
            if updated.status == TaskStatus.FAILED:
                return StageOutcome(
                    task_id=updated.task_id,
                    stage_executed=context.current_stage,
                    next_stage=None,
                    task_status=updated.status,
                    current_owner=updated.current_owner,
                    published_artifacts=(),
                    message=str(exc),
                )
            raise

    def _advance_intake(self) -> StageOutcome:
        next_stage = fixed_next_stage(Stage.INTAKE)
        assert next_stage is not None
        self.task_store.update_runtime_state(
            stage=next_stage,
            current_owner=owner_for_stage(next_stage),
            status=TaskStatus.RUNNING,
        )
        self.task_store.append_event(
            actor=constants.ROLE_ORCHESTRATOR,
            action="intake_routed_to_product_owner",
            result="ok",
        )
        self.task_store.append_recovery_note(
            f"{_utc_now_iso()} intake complete; next_stage={next_stage.value}",
        )
        return self._outcome(
            stage_executed=Stage.INTAKE,
            next_stage=next_stage,
            message="intake complete",
        )

    def _execute_product_owner_stage(self, *, stage: Stage) -> StageOutcome:
        context = self.task_store.load_task_context()
        events = self.task_store.list_events()
        unresolved_feedback = has_pending_human_advice(artifact_store=self.artifact_store)
        role, invocation_result, response_text = self._invoke_role_stage(
            stage=stage,
            unresolved_feedback=unresolved_feedback,
        )
        try:
            decision = _parse_route_decision(
                response_text,
                stage=stage,
                unresolved_feedback=unresolved_feedback,
                current_artifacts=context.current_artifacts,
            )
        except ValueError as exc:
            return self._fail_stage_contract(
                stage=stage,
                published_paths=(),
                message=str(exc),
            )

        try:
            plan_snapshot = _require_plan_snapshot(
                response_text=response_text,
                source_label=f"{stage.value} response",
            )
            self._validate_route_decision_constraints(
                context=context,
                stage=stage,
                decision=decision,
                plan_snapshot=plan_snapshot,
            )
        except ValueError as exc:
            return self._fail_stage_contract(
                stage=stage,
                published_paths=(),
                message=str(exc),
            )

        repair_loop_increment: RepairLoopIncrement | None = None
        if stage == Stage.PRODUCT_OWNER_DISPATCH:
            try:
                repair_loop_increment = self._validate_repair_loop_increment(
                    context=context,
                    decision=decision,
                )
            except ValueError as exc:
                return self._fail_stage_contract(
                    stage=stage,
                    published_paths=(),
                    message=str(exc),
                    action_suffix="route_decision_invalid",
                )

        published_paths = self._publish_role_stage_success(
            stage=stage,
            role=role,
            invocation_result=invocation_result,
            response_text=response_text,
            artifact_plans=_artifact_plans_for_product_owner_stage(
                stage=stage,
                decision=decision,
            ),
        )
        progress_path = self._sync_product_owner_progress(
            stage=stage,
            decision=decision,
            response_text=response_text,
        )
        if progress_path is not None:
            published_paths = (*published_paths, progress_path)
        self._record_route_decision(
            stage=stage,
            context=context,
            decision=decision,
            published_paths=published_paths,
        )
        if unresolved_feedback:
            self._record_human_advice_disposition(
                decision=decision,
                published_paths=published_paths,
            )
            resolve_pending_human_advice(
                task_store=self.task_store,
                artifact_store=self.artifact_store,
                disposition=decision.human_advice_disposition,
            )
        if repair_loop_increment is not None:
            self._record_repair_loop_increment(
                context=context,
                decision=decision,
                increment=repair_loop_increment,
                published_paths=published_paths,
            )

        return self._apply_route_decision(
            stage=stage,
            decision=decision,
            published_paths=published_paths,
            plan_snapshot=plan_snapshot,
        )

    def _execute_architect_planning(self) -> StageOutcome:
        role, invocation_result, response_text = self._invoke_role_stage(
            stage=Stage.ARCHITECT_PLANNING,
        )
        try:
            _require_plan_snapshot(
                response_text=response_text,
                source_label="architect response",
            )
        except ValueError as exc:
            return self._fail_stage_contract(
                stage=Stage.ARCHITECT_PLANNING,
                published_paths=(),
                message=str(exc),
            )
        published_paths = self._publish_role_stage_success(
            stage=Stage.ARCHITECT_PLANNING,
            role=role,
            invocation_result=invocation_result,
            response_text=response_text,
            artifact_plans=(
                _ArtifactPlan(
                    artifact_type=constants.ARTIFACT_PLAN,
                    producer=constants.ROLE_ARCHITECT,
                    consumer=constants.ROLE_PRODUCT_OWNER,
                    status="final",
                    title="Plan",
                ),
            ),
        )
        next_stage = fixed_next_stage(Stage.ARCHITECT_PLANNING)
        assert next_stage is not None
        self.task_store.update_runtime_state(
            stage=next_stage,
            current_owner=owner_for_stage(next_stage),
            status=TaskStatus.RUNNING,
        )
        return self._outcome(
            stage_executed=Stage.ARCHITECT_PLANNING,
            next_stage=next_stage,
            published_artifacts=published_paths,
            message="planning completed",
        )

    def _execute_developer(self) -> StageOutcome:
        role, invocation_result, response_text = self._invoke_role_stage(
            stage=Stage.DEVELOPER,
        )
        published_paths = self._publish_role_stage_success(
            stage=Stage.DEVELOPER,
            role=role,
            invocation_result=invocation_result,
            response_text=response_text,
            artifact_plans=(
                _ArtifactPlan(
                    artifact_type=constants.ARTIFACT_IMPLEMENTATION_RESULT,
                    producer=constants.ROLE_DEVELOPER,
                    consumer=constants.ROLE_PRODUCT_OWNER,
                    status="final",
                    title="Implementation Result",
                ),
            ),
        )
        next_stage = fixed_next_stage(Stage.DEVELOPER)
        assert next_stage is not None
        self.task_store.update_runtime_state(
            stage=next_stage,
            current_owner=owner_for_stage(next_stage),
            status=TaskStatus.RUNNING,
        )
        return self._outcome(
            stage_executed=Stage.DEVELOPER,
            next_stage=next_stage,
            published_artifacts=published_paths,
            message="development completed",
        )

    def _execute_tester(self) -> StageOutcome:
        role, invocation_result, response_text = self._invoke_role_stage(
            stage=Stage.TESTER,
        )
        published_paths = self._publish_role_stage_success(
            stage=Stage.TESTER,
            role=role,
            invocation_result=invocation_result,
            response_text=response_text,
            artifact_plans=(
                _ArtifactPlan(
                    artifact_type=constants.ARTIFACT_TEST_REPORT,
                    producer=constants.ROLE_TESTER,
                    consumer=constants.ROLE_PRODUCT_OWNER,
                    status="final",
                    title="Test Report",
                ),
            ),
        )

        next_stage = fixed_next_stage(Stage.TESTER)
        assert next_stage is not None
        self.task_store.update_runtime_state(
            stage=next_stage,
            current_owner=owner_for_stage(next_stage),
            status=TaskStatus.RUNNING,
        )
        return self._outcome(
            stage_executed=Stage.TESTER,
            next_stage=next_stage,
            published_artifacts=published_paths,
            message="tester completed",
        )

    def _invoke_role_stage(
        self,
        *,
        stage: Stage,
        unresolved_feedback: bool = False,
    ) -> tuple[str, AgentInvocationResult, str]:
        role = owner_for_stage(stage)
        self.task_store.update_runtime_state(
            stage=stage,
            current_owner=role,
            status=TaskStatus.RUNNING,
        )
        invocation = self._build_stage_invocation(
            stage=stage,
            unresolved_feedback=unresolved_feedback,
        )
        invocation_result = self.agent_adapter.invoke(invocation)
        run_output_paths = (
            invocation_result.prompt_path,
            invocation_result.response_path,
            invocation_result.run_log_path,
        )
        if invocation_result.agent_result.execution_status != AgentExecutionStatus.SUCCEEDED:
            self.task_store.update_runtime_state(
                stage=stage,
                current_owner=role,
                status=TaskStatus.FAILED,
            )
            self.task_store.append_event(
                actor=role,
                action=f"{stage.value}_execution_failed",
                input_artifacts=invocation_result.input_artifacts,
                output_artifacts=run_output_paths,
                result="failed",
                notes=invocation_result.agent_result.summary,
            )
            self.task_store.append_recovery_note(
                f"{_utc_now_iso()} stage failed: {stage.value}; reason={invocation_result.agent_result.summary}",
            )
            raise RuntimeError(f"stage failed: {stage.value}")

        response_text = (self.task_workspace_path / invocation_result.response_path).read_text(
            encoding="utf-8",
        )
        return role, invocation_result, response_text

    def _build_stage_invocation(
        self,
        *,
        stage: Stage,
        unresolved_feedback: bool = False,
    ) -> AgentInvocation:
        role = owner_for_stage(stage)
        context = self.task_store.load_task_context()
        input_artifacts: tuple[str, ...] | None = None
        extra_context: dict[str, str] = {}
        include_all_current_artifacts = False

        if stage == Stage.ARCHITECT_PLANNING:
            include_all_current_artifacts = True
        elif stage == Stage.PRODUCT_OWNER_DISPATCH:
            input_artifacts, extra_context = self._product_owner_dispatch_input_artifacts(
                context=context,
            )

        if stage == Stage.DEVELOPER:
            input_artifacts, extra_context = self._developer_input_artifacts(context=context)

        return AgentInvocation(
            role=role,
            stage=stage,
            objective=_stage_objective(
                stage=stage,
                unresolved_feedback=unresolved_feedback,
            ),
            input_artifacts=input_artifacts,
            extra_context=extra_context,
            include_all_current_artifacts=include_all_current_artifacts,
        )

    def _product_owner_dispatch_input_artifacts(
        self,
        *,
        context: TaskContext,
    ) -> tuple[tuple[str, ...] | None, dict[str, str]]:
        if constants.ARTIFACT_PLAN in context.current_artifacts:
            return None, {}

        bootstrap_plan_source_path = context.bootstrap_plan_source_path
        if bootstrap_plan_source_path is None:
            return None, {}

        extra_context = {
            "bootstrap_plan_source": bootstrap_plan_source_path,
            "bootstrap_plan_usage": (
                "Use this external plan only as bootstrap context. Do not treat it as a current artifact "
                "and do not include it in `- based_on_artifacts`."
            ),
        }
        return (bootstrap_plan_source_path,), extra_context

    def _developer_input_artifacts(
        self,
        *,
        context: TaskContext,
    ) -> tuple[tuple[str, ...], dict[str, str]]:
        references = [
            constants.ARTIFACT_PLAN,
            constants.ARTIFACT_DEV_HANDOFF,
        ]
        selected_context_artifacts, extra_context = self._developer_context_artifacts(context=context)
        references.extend(selected_context_artifacts)
        return tuple(references), extra_context

    def _developer_context_artifacts(
        self,
        *,
        context: TaskContext,
    ) -> tuple[tuple[str, ...], dict[str, str]]:
        if constants.ARTIFACT_DEV_HANDOFF not in context.current_artifacts:
            return (), {}

        handoff_document = self.artifact_store.read_current_artifact(constants.ARTIFACT_DEV_HANDOFF)
        raw_value, warning = _extract_developer_context_artifacts_field(handoff_document.body)
        if raw_value is None:
            if warning is None:
                return (), {}
            return (), {"context_artifacts_warning": warning}

        parsed_context_artifacts = _parse_context_artifact_list(
            raw_value,
            current_artifacts=context.current_artifacts,
        )
        extra_context = {
            "resolved_context_artifacts": ", ".join(parsed_context_artifacts)
            if parsed_context_artifacts
            else "-",
        }
        if warning is not None:
            extra_context["context_artifacts_warning"] = warning
        return parsed_context_artifacts, extra_context

    def _validate_route_decision_constraints(
        self,
        *,
        context: TaskContext,
        stage: Stage,
        decision: RouteDecision,
        plan_snapshot: PlanSnapshot,
    ) -> None:
        if decision.task_status != TaskStatus.RUNNING:
            if decision.review_kind_requested is not None:
                raise ValueError("review_kind_requested must be `-` when task_status is terminated.")
            return

        if decision.next_stage != Stage.HUMAN_GATE and decision.review_kind_requested is not None:
            raise ValueError("review_kind_requested must be `-` unless next_stage is human_gate.")

        if stage != Stage.PRODUCT_OWNER_DISPATCH or decision.next_stage is None:
            return

        if decision.next_stage in {Stage.DEVELOPER, Stage.TESTER}:
            if plan_snapshot.human_confirmation_required and not self._has_approved_plan_review(
                plan_revision=plan_snapshot.plan_revision,
            ):
                raise ValueError(
                    "current plan requires human confirmation before routing to developer or tester.",
                )

        if decision.next_stage == Stage.HUMAN_GATE:
            if decision.review_kind_requested == ReviewKind.PLAN:
                if not plan_snapshot.human_confirmation_required:
                    raise ValueError(
                        "cannot request plan review when human_confirmation_required is `no` in plan.",
                    )
                if not plan_snapshot.human_confirmation_items:
                    raise ValueError(
                        "plan review requires non-empty human_confirmation_items in plan.",
                    )
                return
            if decision.review_kind_requested == ReviewKind.DELIVERY:
                for required_artifact in (
                    constants.ARTIFACT_IMPLEMENTATION_RESULT,
                    constants.ARTIFACT_TEST_REPORT,
                ):
                    if required_artifact not in context.current_artifacts:
                        raise ValueError(
                            f"delivery review requires current artifact: {required_artifact}.",
                        )
                return
            raise ValueError("human_gate route requires review_kind_requested to be `plan` or `delivery`.")

        if decision.next_stage == Stage.CLOSEOUT and not self._has_approved_delivery_review():
            raise ValueError(
                "closeout requires the latest current review_decision to be approved for delivery.",
            )

    def _has_approved_plan_review(self, *, plan_revision: str) -> bool:
        review_decision = read_current_review_decision(artifact_store=self.artifact_store)
        if review_decision is None:
            return False
        return (
            review_decision.review_kind == ReviewKind.PLAN
            and review_decision.plan_revision == plan_revision
            and review_decision.decision == constants.REVIEW_DECISION_APPROVED
        )

    def _has_approved_delivery_review(self) -> bool:
        review_decision = read_current_review_decision(artifact_store=self.artifact_store)
        if review_decision is None:
            return False
        return (
            review_decision.review_kind == ReviewKind.DELIVERY
            and review_decision.decision == constants.REVIEW_DECISION_APPROVED
        )

    def _publish_role_stage_success(
        self,
        *,
        stage: Stage,
        role: str,
        invocation_result: AgentInvocationResult,
        response_text: str,
        artifact_plans: tuple[_ArtifactPlan, ...],
    ) -> tuple[str, ...]:
        run_output_paths = (
            invocation_result.prompt_path,
            invocation_result.response_path,
            invocation_result.run_log_path,
        )
        published_paths: list[str] = []
        for plan in artifact_plans:
            publication = self.artifact_store.publish_artifact(
                artifact_type=plan.artifact_type,
                body=_render_agent_artifact_body(
                    plan=plan,
                    stage=stage,
                    role=role,
                    invocation_result=invocation_result,
                    response_text=response_text,
                ),
                stage=stage,
                producer=plan.producer,
                consumer=plan.consumer,
                status=plan.status,
            )
            self.task_store.set_current_artifact(
                artifact_type=plan.artifact_type,
                artifact_path=publication.current_path,
            )
            published_paths.append(publication.current_path)

        self.task_store.append_event(
            actor=role,
            action=f"{stage.value}_execution_succeeded",
            input_artifacts=invocation_result.input_artifacts,
            output_artifacts=(*run_output_paths, *published_paths),
            result="ok",
            notes=invocation_result.agent_result.summary,
        )
        self.task_store.append_recovery_note(
            f"{_utc_now_iso()} stage completed: {stage.value}; artifacts={','.join(published_paths) if published_paths else '-'}",
        )
        return tuple(published_paths)

    def _record_route_decision(
        self,
        *,
        stage: Stage,
        context: TaskContext,
        decision: RouteDecision,
        published_paths: tuple[str, ...],
    ) -> None:
        route_decision_path = _artifact_path_for_type(
            published_paths,
            constants.ARTIFACT_ROUTE_DECISION,
        )
        next_stage = decision.next_stage.value if decision.next_stage is not None else "-"
        self.task_store.append_event(
            actor=constants.ROLE_PRODUCT_OWNER,
            action=f"{stage.value}_route_decision_adopted",
            input_artifacts=tuple(
                context.current_artifacts[artifact_type]
                for artifact_type in decision.based_on_artifacts
                if artifact_type in context.current_artifacts
            ),
            output_artifacts=((route_decision_path,) if route_decision_path else ()),
            result=decision.task_status.value,
            notes=(
                f"next_stage={next_stage}; "
                f"human_advice_disposition={decision.human_advice_disposition.value}; "
                f"review_kind_requested={decision.review_kind_requested.value if decision.review_kind_requested is not None else '-'}"
            ),
        )

    def _record_human_advice_disposition(
        self,
        *,
        decision: RouteDecision,
        published_paths: tuple[str, ...],
    ) -> None:
        route_decision_path = _artifact_path_for_type(
            published_paths,
            constants.ARTIFACT_ROUTE_DECISION,
        )
        self.task_store.append_event(
            actor=constants.ROLE_PRODUCT_OWNER,
            action="human_advice_disposition_recorded",
            output_artifacts=((route_decision_path,) if route_decision_path else ()),
            result=decision.human_advice_disposition.value,
        )

    def _validate_repair_loop_increment(
        self,
        *,
        context: TaskContext,
        decision: RouteDecision,
    ) -> RepairLoopIncrement | None:
        if decision.task_status != TaskStatus.RUNNING or decision.next_stage != Stage.DEVELOPER:
            return None

        reasons: list[str] = []
        if constants.ARTIFACT_REPAIR_TICKET in decision.based_on_artifacts:
            reasons.append(constants.ARTIFACT_REPAIR_TICKET)
        if constants.ARTIFACT_TEST_REPORT in decision.based_on_artifacts:
            reasons.append(constants.ARTIFACT_TEST_REPORT)

        if not reasons:
            return None

        next_loop_count = context.repair_loop_count + 1
        if next_loop_count > self.max_repair_loops:
            raise ValueError(
                "route_decision exceeds repair loop limit: "
                f"next_count={next_loop_count} max={self.max_repair_loops}",
            )
        return RepairLoopIncrement(
            loop_count=next_loop_count,
            reasons=tuple(dict.fromkeys(reasons)),
        )

    def _record_repair_loop_increment(
        self,
        *,
        context: TaskContext,
        decision: RouteDecision,
        increment: RepairLoopIncrement,
        published_paths: tuple[str, ...],
    ) -> None:
        tracked_artifacts = tuple(
            context.current_artifacts[artifact_type]
            for artifact_type in increment.reasons
            if artifact_type in context.current_artifacts
        )
        route_decision_path = _artifact_path_for_type(
            published_paths,
            constants.ARTIFACT_ROUTE_DECISION,
        )
        self.task_store.append_event(
            actor=constants.ROLE_ORCHESTRATOR,
            action="repair_loop_incremented",
            input_artifacts=tracked_artifacts,
            output_artifacts=((route_decision_path,) if route_decision_path else ()),
            result="ok",
            notes=(
                f"repair_loop_count={increment.loop_count}/{self.max_repair_loops}; "
                f"reasons={','.join(increment.reasons)}"
            ),
        )

    def _apply_route_decision(
        self,
        *,
        stage: Stage,
        decision: RouteDecision,
        published_paths: tuple[str, ...],
        plan_snapshot: PlanSnapshot,
    ) -> StageOutcome:
        if decision.task_status == TaskStatus.RUNNING:
            assert decision.next_stage is not None
            if decision.next_stage == Stage.HUMAN_GATE:
                self.task_store.update_runtime_state(
                    stage=Stage.HUMAN_GATE,
                    current_owner=owner_for_stage(Stage.HUMAN_GATE),
                    status=TaskStatus.WAITING_APPROVAL,
                )
                assert decision.review_kind_requested is not None
                review_kind = decision.review_kind_requested
                if review_kind == ReviewKind.PLAN:
                    review_summary = "Product Owner requested human confirmation on the current plan revision."
                    proposal_body = self._plan_review_request_proposal_body()
                    focus_items = plan_snapshot.human_confirmation_items
                    plan_revision = plan_snapshot.plan_revision
                else:
                    review_summary = "Product Owner requested human review on the current delivery proposal."
                    proposal_body = self._delivery_review_request_proposal_body(published_paths)
                    focus_items = ()
                    plan_revision = None
                review_request_id = publish_review_request(
                    task_store=self.task_store,
                    artifact_store=self.artifact_store,
                    summary=review_summary,
                    proposal_body=proposal_body,
                    review_kind=review_kind,
                    plan_revision=plan_revision,
                    focus_items=focus_items,
                )
                self.task_store.append_event(
                    actor=constants.ROLE_ORCHESTRATOR,
                    action="human_gate_awaiting_approval",
                    input_artifacts=published_paths,
                    result="waiting_approval",
                    notes=(
                        f"review_request_id={review_request_id}; "
                        f"review_kind={review_kind.value}; "
                        f"plan_revision={plan_revision or '-'}"
                    ),
                )
                return self._outcome(
                    stage_executed=stage,
                    next_stage=Stage.HUMAN_GATE,
                    published_artifacts=published_paths,
                    message="waiting for human review",
                )
            self.task_store.update_runtime_state(
                stage=decision.next_stage,
                current_owner=owner_for_stage(decision.next_stage),
                status=TaskStatus.RUNNING,
            )
            return self._outcome(
                stage_executed=stage,
                next_stage=decision.next_stage,
                published_artifacts=published_paths,
                message=f"route decision applied: {decision.next_stage.value}",
            )

        self.task_store.update_runtime_state(
            stage=stage,
            current_owner=owner_for_stage(stage),
            status=TaskStatus.TERMINATED,
        )
        return self._outcome(
            stage_executed=stage,
            next_stage=None,
            published_artifacts=published_paths,
            message="task terminated by route decision",
        )

    def _publish_repair_ticket(
        self,
        *,
        role: str,
        response_text: str,
        repair_loop_count: int,
    ) -> ArtifactPublication:
        body = "\n".join(
            [
                "# Repair Ticket",
                "",
                "## Ticket Meta",
                "",
                f"- raised_by_role: {role}",
                f"- source_stage: {Stage.TESTER.value}",
                f"- repair_loop_attempt: {repair_loop_count + 1}",
                f"- max_repair_loops: {self.max_repair_loops}",
                "",
                "## Problem Statement",
                "",
                response_text.strip() or "- empty tester response",
                "",
            ],
        )
        publication = self.artifact_store.publish_artifact(
            artifact_type=constants.ARTIFACT_REPAIR_TICKET,
            body=body,
            stage=Stage.TESTER,
            producer=role,
            consumer=constants.ROLE_PRODUCT_OWNER,
            status="needs_repair",
        )
        self.task_store.set_current_artifact(
            artifact_type=constants.ARTIFACT_REPAIR_TICKET,
            artifact_path=publication.current_path,
        )
        return publication

    def _publish_closeout_and_complete(self, *, stage: Stage) -> StageOutcome:
        context = self.task_store.load_task_context()
        body_lines: list[str] = [
            "# Closeout Report",
            "",
            "## Outcome Summary",
            "",
            f"- task_id: {context.task_id}",
            f"- completed_at: {_utc_now_iso()}",
            "",
            "## Delivered Artifacts",
            "",
        ]
        if context.current_artifacts:
            for artifact_type in sorted(context.current_artifacts):
                if artifact_type == constants.ARTIFACT_CLOSEOUT:
                    continue
                body_lines.append(f"- {artifact_type}: {context.current_artifacts[artifact_type]}")
        else:
            body_lines.append("- none")
        body_lines.extend(
            [
                "",
                "## Verification Summary",
                "",
                f"- repair_loop_count: {context.repair_loop_count}",
                "",
                "## Follow-up Work",
                "",
                "- none",
                "",
            ],
        )

        publication = self.artifact_store.publish_artifact(
            artifact_type=constants.ARTIFACT_CLOSEOUT,
            body="\n".join(body_lines),
            stage=Stage.CLOSEOUT,
            producer=constants.ROLE_ORCHESTRATOR,
            consumer="human",
            status="final",
        )
        self.task_store.set_current_artifact(
            artifact_type=constants.ARTIFACT_CLOSEOUT,
            artifact_path=publication.current_path,
        )
        self.task_store.update_runtime_state(
            stage=Stage.CLOSEOUT,
            current_owner=owner_for_stage(Stage.CLOSEOUT),
            status=TaskStatus.COMPLETED,
        )
        self.task_store.append_event(
            actor=constants.ROLE_ORCHESTRATOR,
            action="closeout_completed",
            output_artifacts=(publication.current_path,),
            result="ok",
            notes=f"from_stage={stage.value}",
        )
        return self._outcome(
            stage_executed=stage,
            next_stage=Stage.CLOSEOUT,
            published_artifacts=(publication.current_path,),
            message="pipeline completed",
        )

    def _sync_product_owner_progress(
        self,
        *,
        stage: Stage,
        decision: RouteDecision,
        response_text: str,
    ) -> str | None:
        snapshot = read_progress_snapshot(artifact_store=self.artifact_store)
        progress_text = _extract_markdown_section(response_text, "Progress") or response_text
        latest_update = _extract_optional_bullet_field(progress_text, "latest_update")
        current_focus = _extract_optional_bullet_field(progress_text, "current_focus")
        next_step = _extract_optional_bullet_field(progress_text, "next_step")
        risks = _extract_optional_bullet_field(progress_text, "risks")
        user_summary = _extract_optional_bullet_field(progress_text, "user_summary")
        needs_review_raw = _extract_optional_bullet_field(progress_text, "needs_human_review")

        resolved_latest_update = latest_update or f"Product Owner updated the task plan during {stage.value}."
        resolved_current_focus = current_focus or self._normalize_progress_value(snapshot.current_focus)
        resolved_next_step = next_step or self._product_owner_next_step(decision=decision)
        resolved_risks = risks or self._normalize_progress_value(snapshot.risks) or "-"
        resolved_needs_review = _parse_optional_yes_no(
            needs_review_raw,
            default=decision.next_stage == Stage.HUMAN_GATE,
            field_name="needs_human_review",
        )
        resolved_user_summary = user_summary or self._compose_progress_user_summary(
            stage=stage,
            current_focus=resolved_current_focus,
            next_step=resolved_next_step,
            risks=resolved_risks,
            needs_human_review=resolved_needs_review,
            latest_update=resolved_latest_update,
        )
        return publish_progress_update(
            task_store=self.task_store,
            artifact_store=self.artifact_store,
            latest_update=resolved_latest_update,
            timeline_title="Product Owner Progress Updated",
            timeline_body=resolved_user_summary,
            current_focus=resolved_current_focus,
            next_step=resolved_next_step,
            risks=resolved_risks,
            needs_human_review=resolved_needs_review,
            user_summary=resolved_user_summary,
        )

    def _product_owner_next_step(self, *, decision: RouteDecision) -> str:
        if decision.task_status == TaskStatus.TERMINATED or decision.next_stage is None:
            return "No further automatic steps."
        if decision.next_stage == Stage.HUMAN_GATE:
            if decision.review_kind_requested == ReviewKind.PLAN:
                return "Await formal human confirmation for the current plan revision."
            return "Await formal human review for the current delivery proposal."
        return f"Route the task to {decision.next_stage.value}."

    def _compose_progress_user_summary(
        self,
        *,
        stage: Stage,
        current_focus: str | None,
        next_step: str | None,
        risks: str,
        needs_human_review: bool,
        latest_update: str,
    ) -> str:
        focus = current_focus or "The Product Owner is organizing the current task."
        next_action = next_step or "The orchestrator will continue with the next stage automatically."
        risk_text = risks if risks and risks != "-" else "No major risks are highlighted right now."
        review_text = "Human review is required." if needs_human_review else "Human review is not required right now."
        return (
            f"Current stage: {stage.value}. {latest_update} Focus: {focus} "
            f"Next: {next_action} Risks: {risk_text} {review_text}"
        )

    def _normalize_progress_value(self, value: str) -> str | None:
        normalized = value.strip()
        if not normalized or normalized == "-":
            return None
        return normalized

    def _fail_stage_contract(
        self,
        *,
        stage: Stage,
        published_paths: tuple[str, ...],
        message: str,
        action_suffix: str = "contract_invalid",
    ) -> StageOutcome:
        self.task_store.update_runtime_state(
            stage=stage,
            current_owner=owner_for_stage(stage),
            status=TaskStatus.FAILED,
        )
        self.task_store.append_event(
            actor=constants.ROLE_ORCHESTRATOR,
            action=f"{stage.value}_{action_suffix}",
            input_artifacts=published_paths,
            result="failed",
            notes=message,
        )
        self.task_store.append_recovery_note(f"{_utc_now_iso()} {message}")
        return self._outcome(
            stage_executed=stage,
            next_stage=None,
            published_artifacts=published_paths,
            message=message,
        )

    def _fail_due_to_invalid_decision(
        self,
        *,
        stage: Stage,
        published_paths: tuple[str, ...],
        message: str,
    ) -> StageOutcome:
        return self._fail_stage_contract(
            stage=stage,
            published_paths=published_paths,
            message=message,
            action_suffix="decision_invalid",
        )

    def _outcome(
        self,
        *,
        stage_executed: Stage,
        next_stage: Stage | None,
        message: str,
        published_artifacts: tuple[str, ...] = (),
    ) -> StageOutcome:
        context = self.task_store.load_task_context()
        return StageOutcome(
            task_id=context.task_id,
            stage_executed=stage_executed,
            next_stage=next_stage,
            task_status=context.status,
            current_owner=context.current_owner,
            published_artifacts=published_artifacts,
            message=message,
        )


    def _delivery_review_request_proposal_body(self, published_paths: tuple[str, ...]) -> str:
        route_decision_path = _artifact_path_for_type(published_paths, constants.ARTIFACT_ROUTE_DECISION)
        if route_decision_path is not None:
            document = self.artifact_store.read_current_artifact(constants.ARTIFACT_ROUTE_DECISION)
            return document.body.strip() or "-"
        return "Review the latest proposal and decide whether it is acceptable."

    def _plan_review_request_proposal_body(self) -> str:
        try:
            document = self.artifact_store.read_current_artifact(constants.ARTIFACT_PLAN)
        except Exception:
            return "Review the latest plan revision and decide whether the pending confirmation items are acceptable."
        return document.body.strip() or "-"


def _artifact_plans_for_product_owner_stage(
    *,
    stage: Stage,
    decision: RouteDecision,
) -> tuple[_ArtifactPlan, ...]:
    if stage == Stage.PRODUCT_OWNER_REFINEMENT:
        plan_consumer = (
            constants.ROLE_ARCHITECT
            if decision.task_status == TaskStatus.RUNNING
            and decision.next_stage == Stage.ARCHITECT_PLANNING
            else constants.ROLE_PRODUCT_OWNER
        )
        return (
            _ArtifactPlan(
                artifact_type=constants.ARTIFACT_PLAN,
                producer=constants.ROLE_PRODUCT_OWNER,
                consumer=plan_consumer,
                status="draft",
                title="Plan",
            ),
            _ArtifactPlan(
                artifact_type=constants.ARTIFACT_ROUTE_DECISION,
                producer=constants.ROLE_PRODUCT_OWNER,
                consumer=constants.ROLE_ORCHESTRATOR,
                status="final",
                title="Route Decision",
            ),
        )
    if stage == Stage.PRODUCT_OWNER_DISPATCH:
        plans: list[_ArtifactPlan] = [
            _ArtifactPlan(
                artifact_type=constants.ARTIFACT_PLAN,
                producer=constants.ROLE_PRODUCT_OWNER,
                consumer=(
                    owner_for_stage(decision.next_stage)
                    if decision.task_status == TaskStatus.RUNNING
                    and decision.next_stage in {Stage.ARCHITECT_PLANNING, Stage.DEVELOPER, Stage.TESTER}
                    else constants.ROLE_PRODUCT_OWNER
                ),
                status="ready",
                title="Plan",
            ),
        ]
        if decision.task_status == TaskStatus.RUNNING and decision.next_stage == Stage.DEVELOPER:
            plans.append(
                _ArtifactPlan(
                    artifact_type=constants.ARTIFACT_DEV_HANDOFF,
                    producer=constants.ROLE_PRODUCT_OWNER,
                    consumer=constants.ROLE_DEVELOPER,
                    status="ready",
                    title="Developer Handoff",
                ),
            )
        if decision.task_status == TaskStatus.RUNNING and decision.next_stage == Stage.TESTER:
            plans.append(
                _ArtifactPlan(
                    artifact_type=constants.ARTIFACT_TEST_HANDOFF,
                    producer=constants.ROLE_PRODUCT_OWNER,
                    consumer=constants.ROLE_TESTER,
                    status="ready",
                    title="Tester Handoff",
                ),
            )
        plans.append(
            _ArtifactPlan(
                artifact_type=constants.ARTIFACT_ROUTE_DECISION,
                producer=constants.ROLE_PRODUCT_OWNER,
                consumer=constants.ROLE_ORCHESTRATOR,
                status="final",
                title="Route Decision",
            ),
        )
        return tuple(plans)
    raise ValueError(f"unsupported Product Owner route stage: {stage.value}")


def _render_agent_artifact_body(
    *,
    plan: _ArtifactPlan,
    stage: Stage,
    role: str,
    invocation_result: AgentInvocationResult,
    response_text: str,
) -> str:
    normalized_response = response_text.strip() or "- empty response from agent invocation"
    return "\n".join(
        [
            f"# {plan.title}",
            "",
            "## Stage Context",
            "",
            f"- stage: {stage.value}",
            f"- role: {role}",
            f"- run_directory: {invocation_result.run_directory}",
            f"- source_response: {invocation_result.response_path}",
            "",
            "## Agent Response",
            "",
            normalized_response,
            "",
        ],
    )


def _stage_objective(
    *,
    stage: Stage,
    unresolved_feedback: bool,
) -> str:
    if stage == Stage.PRODUCT_OWNER_REFINEMENT:
        objective = (
            "Refine the current plan baseline, refresh the user-facing progress summary, and emit "
            "both the latest plan and a route_decision. Your response must include bullet fields "
            "`- next_stage`, `- task_status`, `- based_on_artifacts`, `- human_advice_disposition`, "
            "and `- review_kind_requested`. It must also include progress bullets `- latest_update`, "
            "`- current_focus`, `- next_step`, `- risks`, `- needs_human_review`, and "
            "`- user_summary`. At this stage, running next_stage must be either "
            "`architect_planning` or `product_owner_dispatch`, and task_status must be "
            "`running` or `terminated`."
        )
    elif stage == Stage.PRODUCT_OWNER_DISPATCH:
        objective = (
            "Refresh the current plan, emit any necessary handoff artifacts, refresh the user-facing "
            "progress summary, and emit a route_decision. Your response must include bullet fields "
            "`- next_stage`, `- task_status`, `- based_on_artifacts`, `- human_advice_disposition`, "
            "and `- review_kind_requested`. It must also include progress bullets `- latest_update`, "
            "`- current_focus`, `- next_step`, `- risks`, `- needs_human_review`, and "
            "`- user_summary`. At this stage, running next_stage must be one of "
            "`architect_planning`, `developer`, `tester`, `human_gate`, or `closeout`, and "
            "task_status must be `running` or `terminated`."
        )
    elif stage == Stage.ARCHITECT_PLANNING:
        objective = "Update the current plan for Product Owner based on the latest task context."
    elif stage == Stage.DEVELOPER:
        objective = "Implement the dispatched plan subtask in the target repository."
    elif stage == Stage.TESTER:
        objective = (
            "Validate implementation and produce a test report with explicit bullet field "
            "`- decision: passed|failed`."
        )
    else:  # pragma: no cover - guarded by StageExecutor dispatch
        raise ValueError(f"unsupported stage objective: {stage.value}")

    if unresolved_feedback:
        objective += (
            " The latest formal human advice is still unresolved, so you must absorb it in this "
            "response and set `- human_advice_disposition` to accepted, partially_accepted, or rejected."
        )
    return objective


def _artifact_path_for_type(
    published_paths: tuple[str, ...],
    artifact_type: str,
) -> str | None:
    suffix = f"/{constants.ARTIFACT_FILENAMES[artifact_type]}"
    for artifact_path in published_paths:
        if artifact_path.endswith(suffix):
            return artifact_path
    return None


def _latest_event_seq(events: tuple[TaskEvent, ...], action: str) -> int:
    latest = 0
    for event in events:
        if event.action == action:
            latest = event.seq
    return latest


def _parse_route_decision(
    response_text: str,
    *,
    stage: Stage,
    unresolved_feedback: bool,
    current_artifacts: dict[str, str],
) -> RouteDecision:
    route_decision_text = _extract_markdown_section(response_text, "Route Decision") or response_text

    next_stage_raw = _extract_required_bullet_field(route_decision_text, "next_stage")
    task_status_raw = _extract_required_bullet_field(route_decision_text, "task_status")
    based_on_raw = _extract_required_bullet_field(route_decision_text, "based_on_artifacts")
    feedback_disposition_raw = _extract_required_bullet_field(
        route_decision_text,
        "human_advice_disposition",
    )
    review_kind_requested_raw = _extract_required_bullet_field(
        route_decision_text,
        "review_kind_requested",
    )

    task_status = _parse_route_task_status(task_status_raw)
    based_on_artifacts = _parse_based_on_artifacts(
        based_on_raw,
        current_artifacts=current_artifacts,
    )
    feedback_disposition = _parse_human_advice_disposition(feedback_disposition_raw)

    if unresolved_feedback:
        if feedback_disposition == HumanAdviceDisposition.NONE:
            raise ValueError(
                "human_advice_disposition must not be `none` while unresolved human advice exists.",
            )
    elif feedback_disposition != HumanAdviceDisposition.NONE:
        raise ValueError(
            "human_advice_disposition must be `none` when no unresolved human advice exists.",
        )

    next_stage: Stage | None
    if task_status == TaskStatus.RUNNING:
        if next_stage_raw.strip() == "-":
            raise ValueError("next_stage must not be `-` when task_status is running.")
        try:
            next_stage = Stage(next_stage_raw.strip())
        except ValueError as exc:
            raise ValueError(f"next_stage must be a valid stage; got {next_stage_raw!r}.") from exc
        allowed_targets = route_targets_for_stage(stage)
        if next_stage not in allowed_targets:
            allowed = ", ".join(target.value for target in allowed_targets)
            raise ValueError(
                f"next_stage must be one of: {allowed} when stage is {stage.value}; "
                f"got {next_stage.value}.",
            )
    else:
        if next_stage_raw.strip() != "-":
            raise ValueError("next_stage must be `-` when task_status is terminated.")
        next_stage = None

    review_kind_requested = _parse_review_kind_requested(
        review_kind_requested_raw,
        next_stage=next_stage,
        task_status=task_status,
    )

    return RouteDecision(
        next_stage=next_stage,
        task_status=task_status,
        based_on_artifacts=based_on_artifacts,
        human_advice_disposition=feedback_disposition,
        review_kind_requested=review_kind_requested,
    )


def _extract_required_bullet_field(response_text: str, field_name: str) -> str:
    matches = _find_bullet_field_matches(response_text, field_name)
    if len(matches) > 1:
        raise ValueError(f"{field_name} must appear exactly once.")
    if matches:
        return matches[0]

    table_pattern = re.compile(
        _TABLE_FIELD_TEMPLATE.format(field_name=re.escape(field_name)),
        flags=re.IGNORECASE | re.MULTILINE,
    )
    if table_pattern.search(response_text) is not None:
        raise ValueError(
            f"{field_name} must be provided as bullet field `- {field_name}: ...`; table format is not accepted.",
        )
    raise ValueError(f"{field_name} must be provided as bullet field `- {field_name}: ...`.")


def _extract_optional_bullet_field(response_text: str, field_name: str) -> str | None:
    matches = _find_bullet_field_matches(response_text, field_name)
    if len(matches) > 1:
        raise ValueError(f"{field_name} must appear at most once.")
    if matches:
        return matches[0]
    return None


def _extract_markdown_section(response_text: str, title: str) -> str | None:
    lines = response_text.splitlines()
    start_index: int | None = None
    heading_level: int | None = None

    for index, line in enumerate(lines):
        stripped = line.strip()
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", stripped)
        if match is None:
            continue
        if match.group(2).strip() != title:
            continue
        start_index = index + 1
        heading_level = len(match.group(1))
        break

    if start_index is None or heading_level is None:
        return None

    collected: list[str] = []
    for line in lines[start_index:]:
        stripped = line.strip()
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", stripped)
        if match is not None and len(match.group(1)) <= heading_level:
            break
        collected.append(line)
    return "\n".join(collected).strip("\n")


def _extract_developer_context_artifacts_field(response_text: str) -> tuple[str | None, str | None]:
    developer_handoff_text = _extract_markdown_section(response_text, "Developer Handoff") or response_text

    raw_value = _extract_optional_bullet_field(developer_handoff_text, "context_artifacts")
    if raw_value is not None:
        return raw_value, None

    legacy_pattern = re.compile(
        r"^-\s*`context_artifacts:\s*(.+?)`\s*$",
        flags=re.IGNORECASE | re.MULTILINE,
    )
    matches = [match.strip() for match in legacy_pattern.findall(developer_handoff_text) if match.strip()]
    if len(matches) > 1:
        raise ValueError("context_artifacts must appear at most once.")
    if matches:
        return (
            matches[0],
            "Parsed legacy backtick-wrapped `context_artifacts` directive from dev_handoff.",
        )

    if "context_artifacts" not in developer_handoff_text:
        return None, None
    return (
        None,
        "Found `context_artifacts` text in dev_handoff but no valid directive; expected `- context_artifacts: ...`.",
    )


def _parse_optional_yes_no(raw_value: str | None, *, default: bool, field_name: str) -> bool:
    if raw_value is None:
        return default
    normalized = raw_value.strip().lower()
    if normalized == "yes":
        return True
    if normalized == "no":
        return False
    raise ValueError(f"{field_name} must be `yes` or `no` when provided.")


def _find_bullet_field_matches(response_text: str, field_name: str) -> list[str]:
    bullet_pattern = re.compile(
        _BULLET_FIELD_TEMPLATE.format(field_name=re.escape(field_name)),
        flags=re.IGNORECASE | re.MULTILINE,
    )
    return [match.strip() for match in bullet_pattern.findall(response_text) if match.strip()]


def _parse_route_task_status(raw_status: str) -> TaskStatus:
    normalized = raw_status.strip().lower().replace("-", "_").replace(" ", "_")
    if normalized == TaskStatus.RUNNING.value:
        return TaskStatus.RUNNING
    if normalized == TaskStatus.TERMINATED.value:
        return TaskStatus.TERMINATED
    raise ValueError(
        "task_status must be one of: running, terminated; "
        f"got {raw_status!r}.",
    )


def _format_current_artifact_types(current_artifacts: dict[str, str]) -> str:
    if not current_artifacts:
        return "-"
    return ", ".join(sorted(current_artifacts))


def _parse_based_on_artifacts(
    raw_value: str,
    *,
    current_artifacts: dict[str, str],
) -> tuple[str, ...]:
    normalized = raw_value.strip()
    if normalized == "-":
        return ()

    parsed: list[str] = []
    seen: set[str] = set()
    allowed_current_artifacts = _format_current_artifact_types(current_artifacts)
    for part in normalized.split(","):
        artifact_type = part.strip()
        if not artifact_type:
            raise ValueError("based_on_artifacts must not contain empty entries.")
        if artifact_type not in constants.ARTIFACT_TYPES:
            raise ValueError(
                "based_on_artifacts contains unknown artifact type: "
                f"{artifact_type!r}. Only current artifact types may be listed here; "
                "`task` and `event_log` are readable system inputs, not valid values. "
                f"Current artifact types for this task: {allowed_current_artifacts}.",
            )
        if artifact_type not in current_artifacts:
            raise ValueError(
                f"based_on_artifacts references unavailable current artifact: {artifact_type!r}. "
                f"Current artifact types for this task: {allowed_current_artifacts}.",
            )
        if artifact_type in seen:
            continue
        seen.add(artifact_type)
        parsed.append(artifact_type)
    return tuple(parsed)


def _parse_human_advice_disposition(raw_value: str) -> HumanAdviceDisposition:
    normalized = raw_value.strip().lower().replace("-", "_").replace(" ", "_")
    try:
        return HumanAdviceDisposition(normalized)
    except ValueError as exc:
        allowed = ", ".join(constants.HUMAN_ADVICE_DISPOSITION_NAMES)
        raise ValueError(
            f"human_advice_disposition must be one of: {allowed}; got {raw_value!r}.",
        ) from exc


def _parse_review_kind_requested(
    raw_value: str,
    *,
    next_stage: Stage | None,
    task_status: TaskStatus,
) -> ReviewKind | None:
    stripped = raw_value.strip()
    if task_status == TaskStatus.TERMINATED:
        if stripped != "-":
            raise ValueError("review_kind_requested must be `-` when task_status is terminated.")
        return None
    if next_stage != Stage.HUMAN_GATE:
        if stripped != "-":
            raise ValueError("review_kind_requested must be `-` unless next_stage is human_gate.")
        return None
    normalized = stripped.lower().replace("-", "_").replace(" ", "_")
    if normalized == ReviewKind.PLAN.value:
        return ReviewKind.PLAN
    if normalized == ReviewKind.DELIVERY.value:
        return ReviewKind.DELIVERY
    allowed = ", ".join(constants.REVIEW_KIND_NAMES)
    raise ValueError(
        f"review_kind_requested must be one of: {allowed} when next_stage is human_gate; got {raw_value!r}.",
    )


def _parse_plan_snapshot(*, response_text: str, source_label: str) -> PlanSnapshot | None:
    plan_text = _extract_markdown_section(response_text, "Plan") or response_text

    plan_revision = _extract_optional_bullet_field(plan_text, "plan_revision")
    human_confirmation_required = _extract_optional_bullet_field(
        plan_text,
        "human_confirmation_required",
    )
    human_confirmation_items = _extract_optional_bullet_field(
        plan_text,
        "human_confirmation_items",
    )
    active_subtask_id = _extract_optional_bullet_field(plan_text, "active_subtask_id")

    has_any_plan_fields = any(
        value is not None
        for value in (
            plan_revision,
            human_confirmation_required,
            human_confirmation_items,
            active_subtask_id,
        )
    )
    if not has_any_plan_fields:
        return None
    if plan_revision is None:
        raise ValueError(f"{source_label} must include `- plan_revision: ...`.")
    if human_confirmation_required is None:
        raise ValueError(f"{source_label} must include `- human_confirmation_required: yes|no`.")
    if human_confirmation_items is None:
        raise ValueError(f"{source_label} must include `- human_confirmation_items: ...`.")

    requires_confirmation = _parse_optional_yes_no(
        human_confirmation_required,
        default=False,
        field_name="human_confirmation_required",
    )
    confirmation_items = _parse_pipe_or_dash_list(
        human_confirmation_items,
        field_name="human_confirmation_items",
    )
    if requires_confirmation and not confirmation_items:
        raise ValueError("human_confirmation_items must be non-empty when human_confirmation_required is `yes`.")
    if not requires_confirmation and confirmation_items:
        raise ValueError("human_confirmation_items must be `-` when human_confirmation_required is `no`.")

    normalized_active_subtask = None
    if active_subtask_id is not None:
        normalized_active_subtask = active_subtask_id.strip()
        if normalized_active_subtask == "-":
            normalized_active_subtask = None

    return PlanSnapshot(
        plan_revision=plan_revision.strip(),
        human_confirmation_required=requires_confirmation,
        human_confirmation_items=confirmation_items,
        active_subtask_id=normalized_active_subtask,
    )


def _require_plan_snapshot(*, response_text: str, source_label: str) -> PlanSnapshot:
    snapshot = _parse_plan_snapshot(response_text=response_text, source_label=source_label)
    if snapshot is None:
        raise ValueError(f"{source_label} must include plan metadata fields for the formal `plan` artifact.")
    return snapshot


def _parse_pipe_or_dash_list(raw_value: str, *, field_name: str) -> tuple[str, ...]:
    normalized = raw_value.strip()
    if normalized == "-":
        return ()
    parts = [part.strip() for part in normalized.split("|")]
    parsed = tuple(part for part in parts if part)
    if not parsed:
        raise ValueError(f"{field_name} must not be empty.")
    return parsed


def _parse_context_artifact_list(
    raw_value: str,
    *,
    current_artifacts: dict[str, str],
) -> tuple[str, ...]:
    normalized = raw_value.strip()
    if normalized == "-":
        return ()

    disallowed_baseline = {
        constants.ARTIFACT_PLAN,
        constants.ARTIFACT_DEV_HANDOFF,
    }
    parsed: list[str] = []
    seen: set[str] = set()
    allowed_current_artifacts = _format_current_artifact_types(current_artifacts)
    for part in normalized.split(","):
        artifact_type = part.strip()
        if not artifact_type:
            raise ValueError("context_artifacts must not contain empty entries.")
        if artifact_type not in constants.ARTIFACT_TYPES:
            raise ValueError(
                "context_artifacts contains unknown artifact type: "
                f"{artifact_type!r}. Current artifact types for this task: {allowed_current_artifacts}.",
            )
        if artifact_type in disallowed_baseline:
            raise ValueError(
                "context_artifacts must not repeat baseline developer inputs: "
                f"{artifact_type!r}.",
            )
        if artifact_type not in current_artifacts:
            raise ValueError(
                f"context_artifacts references unavailable current artifact: {artifact_type!r}. "
                f"Current artifact types for this task: {allowed_current_artifacts}.",
            )
        if artifact_type in seen:
            continue
        seen.add(artifact_type)
        parsed.append(artifact_type)
    return tuple(parsed)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


__all__ = [
    "RouteDecision",
    "StageExecutor",
    "StageOutcome",
]
