"""Role prompt loader and codex-cli adapter with run trace persistence."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
import re
import shutil
import subprocess
from typing import Mapping, Protocol, Sequence

from . import protocol as constants
from .artifact_store import ArtifactStore
from .models import AgentResult
from .protocol import AgentExecutionStatus, AgentResultType, Stage
from .task_store import TaskStore

_SEQUENCE_PREFIX_RE = re.compile(r"^(?P<seq>[0-9]+)_")

_ROLE_PROMPT_FILENAMES: dict[str, str] = {
    constants.ROLE_PRODUCT_OWNER: "product_owner.md",
    constants.ROLE_PROJECT_MANAGER: "project_manager.md",
    constants.ROLE_DEVELOPER: "developer.md",
    constants.ROLE_TESTER: "tester.md",
    constants.ROLE_QA: "qa.md",
}

_ROLE_RESULT_TYPES: dict[str, AgentResultType] = {
    constants.ROLE_PRODUCT_OWNER: AgentResultType.ROUTE_DECISION_RESULT,
    constants.ROLE_PROJECT_MANAGER: AgentResultType.RESEARCH_RESULT,
    constants.ROLE_DEVELOPER: AgentResultType.IMPLEMENTATION_RESULT,
    constants.ROLE_TESTER: AgentResultType.TEST_RESULT,
    constants.ROLE_QA: AgentResultType.QA_RESULT,
}

_ROLE_REQUIRED_ARTIFACTS: dict[str, tuple[str, ...]] = {
    constants.ROLE_PRODUCT_OWNER: (),
    constants.ROLE_PROJECT_MANAGER: (
        constants.ARTIFACT_REQUIREMENT_SPEC,
    ),
    constants.ROLE_DEVELOPER: (
        constants.ARTIFACT_REQUIREMENT_SPEC,
        constants.ARTIFACT_EXECUTION_PLAN,
        constants.ARTIFACT_DEV_HANDOFF,
    ),
    constants.ROLE_TESTER: (
        constants.ARTIFACT_REQUIREMENT_SPEC,
        constants.ARTIFACT_EXECUTION_PLAN,
        constants.ARTIFACT_TEST_HANDOFF,
        constants.ARTIFACT_IMPLEMENTATION_RESULT,
    ),
    constants.ROLE_QA: (
        constants.ARTIFACT_REQUIREMENT_SPEC,
        constants.ARTIFACT_EXECUTION_PLAN,
        constants.ARTIFACT_IMPLEMENTATION_RESULT,
        constants.ARTIFACT_TEST_REPORT,
    ),
}

_PROMPT_REQUIRED_KEYWORDS: tuple[str, ...] = (
    "你的输入",
    "你的输出",
    "你的职责边界",
)

_DEFAULT_CODEX_ARGUMENTS: tuple[str, ...] = (
    "exec",
    "-m",
    "gpt-5-4",
    "--yolo",
    "--cd",
    "{target_repo_path}",
    "{git_repo_check_argument}",
    "{skills_add_dir_flag}",
    "{skills_dir_argument}",
    "-o",
    "{response_path}",
    "{prompt_argument}",
)


class AgentAdapterError(RuntimeError):
    """Base error for role prompt loading and execution adapter."""


class UnsupportedRoleError(AgentAdapterError):
    """Raised when caller requests an unsupported role."""


class PromptAssetError(AgentAdapterError):
    """Raised when role prompt assets are missing or malformed."""


class AgentAdapterContractError(AgentAdapterError):
    """Raised when adapter inputs violate role contract requirements."""


@dataclass(frozen=True)
class RunnerResult:
    """Result payload returned by command runner implementations."""

    command: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str


class Runner(Protocol):
    """Protocol for command runner abstraction used by adapter."""

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        stdin_text: str | None = None,
        timeout_seconds: int | None = None,
    ) -> RunnerResult:
        """Execute one command and return captured stdout/stderr."""


class SubprocessRunner:
    """Default command runner backed by `subprocess.run`."""

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        stdin_text: str | None = None,
        timeout_seconds: int | None = None,
    ) -> RunnerResult:
        completed = subprocess.run(
            list(command),
            cwd=str(cwd),
            input=stdin_text,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
        return RunnerResult(
            command=tuple(str(item) for item in command),
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


def _cwd_default_dir(dirname: str) -> Path:
    return Path.cwd().resolve() / dirname


@dataclass
class AgentInvocation:
    """Input payload for one role invocation."""

    role: str
    objective: str
    stage: Stage | str
    input_artifacts: tuple[str, ...] | list[str] | None = None
    extra_context: Mapping[str, str] = field(default_factory=dict)
    include_all_current_artifacts: bool = False
    strict_required_artifacts: bool = True
    timeout_seconds: int | None = None

    def __post_init__(self) -> None:
        self.role = _normalize_role(self.role)
        self.objective = _normalize_non_empty(self.objective, "objective")
        self.stage = Stage(self.stage)
        if self.input_artifacts is not None:
            if isinstance(self.input_artifacts, tuple):
                values = self.input_artifacts
            elif isinstance(self.input_artifacts, list):
                values = tuple(self.input_artifacts)
            else:
                raise TypeError("input_artifacts must be tuple[str, ...], list[str], or None.")
            self.input_artifacts = tuple(
                _normalize_non_empty(value, "input_artifacts")
                for value in values
            )
        if not isinstance(self.extra_context, Mapping):
            raise TypeError("extra_context must be a mapping.")
        normalized_context: dict[str, str] = {}
        for raw_key, raw_value in self.extra_context.items():
            key = _normalize_non_empty(str(raw_key), "extra_context key")
            value = _normalize_non_empty(str(raw_value), f"extra_context[{key}]")
            normalized_context[key] = value
        self.extra_context = normalized_context
        if not isinstance(self.include_all_current_artifacts, bool):
            raise TypeError("include_all_current_artifacts must be bool.")
        if not isinstance(self.strict_required_artifacts, bool):
            raise TypeError("strict_required_artifacts must be bool.")
        if self.timeout_seconds is not None:
            if not isinstance(self.timeout_seconds, int):
                raise TypeError("timeout_seconds must be int or None.")
            if self.timeout_seconds <= 0:
                raise ValueError("timeout_seconds must be > 0 when provided.")


@dataclass(frozen=True)
class AgentInputDocument:
    """Resolved input markdown artifact included in one prompt."""

    reference: str
    relative_path: str
    content: str


@dataclass(frozen=True)
class AgentInvocationResult:
    """Result payload for one invocation including trace file pointers."""

    invocation: AgentInvocation
    agent_result: AgentResult
    command: tuple[str, ...]
    exit_code: int
    prompt_path: str
    response_path: str
    run_log_path: str
    run_directory: str
    input_artifacts: tuple[str, ...]
    missing_input_artifacts: tuple[str, ...]


class AgentAdapter:
    """Unified adapter that invokes one role through local codex cli."""

    def __init__(
        self,
        task_workspace_path: str | Path,
        *,
        runner: Runner | None = None,
        codex_executable: str = "codex",
        codex_arguments: Sequence[str] = _DEFAULT_CODEX_ARGUMENTS,
        send_prompt_via_stdin: bool = True,
        agents_dir: str | Path | None = None,
        skills_dir: str | Path | None = None,
    ) -> None:
        self.task_workspace_path = Path(task_workspace_path).expanduser().resolve()
        self._resource_stack = ExitStack()
        self.runner = runner or SubprocessRunner()
        self.codex_executable = _normalize_non_empty(codex_executable, "codex_executable")
        if not codex_arguments:
            raise ValueError("codex_arguments must be non-empty.")
        self.codex_arguments = tuple(str(argument) for argument in codex_arguments)
        self.send_prompt_via_stdin = _coerce_bool(
            send_prompt_via_stdin,
            "send_prompt_via_stdin",
        )
        self.agents_dir = self._resolve_agents_dir(agents_dir)
        self.skills_dir = self._resolve_skills_dir(skills_dir)
        self.artifact_store = ArtifactStore(self.task_workspace_path)
        self.task_store = TaskStore(self.task_workspace_path)

    def close(self) -> None:
        """Release any temporary package resource handles."""

        self._resource_stack.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def supported_roles(self) -> tuple[str, ...]:
        """Return supported role names for this adapter."""

        return tuple(_ROLE_PROMPT_FILENAMES)

    def validate_prompt_assets(self) -> dict[str, str]:
        """Validate all role prompt files and return resolved paths."""

        resolved: dict[str, str] = {}
        for role in self.supported_roles():
            _, prompt_path = self.load_role_prompt(role)
            resolved[role] = str(prompt_path)
        return resolved

    def load_role_prompt(self, role: str) -> tuple[str, Path]:
        """Load role prompt markdown from `agents/` directory."""

        normalized_role = _normalize_role(role)
        prompt_filename = _ROLE_PROMPT_FILENAMES.get(normalized_role)
        if prompt_filename is None:
            supported = ", ".join(sorted(self.supported_roles()))
            raise UnsupportedRoleError(f"Unsupported role {normalized_role!r}. Supported: {supported}.")

        prompt_path = self.agents_dir / prompt_filename
        if not prompt_path.is_file():
            raise PromptAssetError(f"Role prompt asset is missing: {prompt_path}")

        content = prompt_path.read_text(encoding="utf-8")
        normalized = content.strip()
        if not normalized:
            raise PromptAssetError(f"Role prompt asset is empty: {prompt_path}")
        if not normalized.startswith("#"):
            raise PromptAssetError(f"Role prompt asset must start with markdown heading: {prompt_path}")

        missing_keywords = [keyword for keyword in _PROMPT_REQUIRED_KEYWORDS if keyword not in normalized]
        if missing_keywords:
            missing = ", ".join(missing_keywords)
            raise PromptAssetError(
                f"Role prompt asset contract mismatch ({missing}) in {prompt_path}.",
            )

        return content, prompt_path

    def invoke(self, invocation: AgentInvocation) -> AgentInvocationResult:
        """Invoke one role and persist prompt/response/run log under `runs/`."""

        task_context = self.task_store.load_task_context()
        role_prompt, prompt_asset_path = self.load_role_prompt(invocation.role)
        run_directory_path = self.artifact_store.allocate_run_directory(role=invocation.role)
        run_directory = self._workspace_relative(run_directory_path)

        prompt_file_path = run_directory_path / "prompt.md"
        response_file_path = run_directory_path / "response.md"
        run_log_path = run_directory_path / "run.log"
        prompt_file_rel = self._workspace_relative(prompt_file_path)
        response_file_rel = self._workspace_relative(response_file_path)
        run_log_rel = self._workspace_relative(run_log_path)

        input_documents, missing_input_artifacts = self._collect_input_documents(invocation)
        prompt_text = self._render_prompt(
            invocation=invocation,
            task_context=task_context,
            role_prompt=role_prompt,
            prompt_asset_path=prompt_asset_path,
            input_documents=input_documents,
            missing_input_artifacts=missing_input_artifacts,
        )
        prompt_file_path.write_text(prompt_text, encoding="utf-8")

        command = self._build_command(
            prompt_file_path=prompt_file_path,
            response_file_path=response_file_path,
            task_context=task_context,
            prompt_text=prompt_text,
        )
        exit_code = 0
        stdout = ""
        stderr = ""
        failure_reason: str | None = None

        if missing_input_artifacts and invocation.strict_required_artifacts:
            missing = ", ".join(missing_input_artifacts)
            failure_reason = f"required input artifacts missing: {missing}"
            exit_code = 2
        elif isinstance(self.runner, SubprocessRunner):
            executable = command[0]
            if shutil.which(executable) is None:
                failure_reason = f"runner executable not found: {executable}"
                exit_code = 127

        if failure_reason is None:
            try:
                runner_result = self.runner.run(
                    command,
                    cwd=Path(task_context.target_repo_path),
                    stdin_text=(prompt_text if self.send_prompt_via_stdin else None),
                    timeout_seconds=invocation.timeout_seconds,
                )
                command = runner_result.command
                exit_code = runner_result.exit_code
                stdout = runner_result.stdout
                stderr = runner_result.stderr
            except Exception as exc:  # pragma: no cover - exercised via fake runner in unit tests
                failure_reason = f"{type(exc).__name__}: {exc}"
                exit_code = -1

        if failure_reason is None and exit_code != 0:
            failure_reason = f"runner exited with non-zero code: {exit_code}"

        response_text = self._resolve_response_text(
            response_file_path=response_file_path,
            stdout=stdout,
            stderr=stderr,
            failure_reason=failure_reason,
        )
        response_file_path.write_text(response_text, encoding="utf-8")

        run_log_text = self._build_run_log_text(
            invocation=invocation,
            task_context=task_context,
            command=command,
            exit_code=exit_code,
            prompt_path=prompt_file_rel,
            response_path=response_file_rel,
            missing_input_artifacts=missing_input_artifacts,
            failure_reason=failure_reason,
            stdout=stdout,
            stderr=stderr,
        )
        run_log_path.write_text(run_log_text, encoding="utf-8")

        execution_status = (
            AgentExecutionStatus.SUCCEEDED
            if failure_reason is None and exit_code == 0
            else AgentExecutionStatus.FAILED
        )
        summary = self._derive_summary(
            role=invocation.role,
            response_text=response_text,
            failure_reason=failure_reason,
        )
        warnings = self._build_warnings(
            missing_input_artifacts=missing_input_artifacts,
            failure_reason=failure_reason,
            stderr=stderr,
        )
        next_actions = (
            ("orchestrator_publish_current_artifact",)
            if execution_status == AgentExecutionStatus.SUCCEEDED
            else ("inspect_run_log_and_fix_inputs", "rerun_role_invocation")
        )

        agent_result = AgentResult(
            target_repo_path=task_context.target_repo_path,
            task_workspace_path=str(self.task_workspace_path),
            artifact_path=response_file_rel,
            version=_parse_run_sequence(run_directory_path.name),
            supersedes=None,
            result_id=f"run-{run_directory_path.name}",
            role=invocation.role,
            result_type=_ROLE_RESULT_TYPES[invocation.role],
            execution_status=execution_status,
            summary=summary,
            next_actions=next_actions,
            warnings=warnings,
            run_directory=run_directory,
        )

        return AgentInvocationResult(
            invocation=invocation,
            agent_result=agent_result,
            command=command,
            exit_code=exit_code,
            prompt_path=prompt_file_rel,
            response_path=response_file_rel,
            run_log_path=run_log_rel,
            run_directory=run_directory,
            input_artifacts=tuple(document.relative_path for document in input_documents),
            missing_input_artifacts=missing_input_artifacts,
        )

    def _resolve_agents_dir(self, agents_dir: str | Path | None) -> Path:
        candidate = self._resolve_optional_dir(agents_dir, default_dirname="agents")
        if not candidate.is_dir():
            raise AgentAdapterError(f"agents directory does not exist: {candidate}")
        return candidate

    def _resolve_skills_dir(self, skills_dir: str | Path | None) -> Path:
        candidate = self._resolve_optional_dir(skills_dir, default_dirname="skills")
        if not candidate.is_dir():
            raise AgentAdapterError(f"skills directory does not exist: {candidate}")
        return candidate

    def _resolve_optional_dir(
        self,
        value: str | Path | None,
        *,
        default_dirname: str,
    ) -> Path:
        if value is None:
            bundled_candidate = self._resolve_bundled_dir(default_dirname)
            if bundled_candidate is not None:
                return bundled_candidate

            local_candidate = _cwd_default_dir(default_dirname)
            if local_candidate.is_dir():
                return local_candidate
            return local_candidate

        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = (Path.cwd().resolve() / candidate).resolve()
        else:
            candidate = candidate.resolve()
        return candidate

    def _resolve_bundled_dir(self, dirname: str) -> Path | None:
        bundled_root = resources.files("xclaw")
        resource = bundled_root.joinpath(dirname)
        if not resource.is_dir():
            return None
        return self._resource_stack.enter_context(resources.as_file(resource)).resolve()

    def _build_command(
        self,
        *,
        prompt_file_path: Path,
        response_file_path: Path,
        task_context: object,
        prompt_text: str,
    ) -> tuple[str, ...]:
        prompt_argument = "-" if self.send_prompt_via_stdin else prompt_text
        values = {
            "prompt_path": str(prompt_file_path),
            "prompt_text": prompt_text,
            "prompt_argument": prompt_argument,
            "workspace_path": str(self.task_workspace_path),
            "target_repo_path": task_context.target_repo_path,
            "response_path": str(response_file_path),
            "skills_add_dir_flag": "--add-dir" if self.skills_dir and self.skills_dir.is_dir() else "",
            "skills_dir_argument": (
                str(self.skills_dir)
                if self.skills_dir and self.skills_dir.is_dir()
                else ""
            ),
            "git_repo_check_argument": (
                ""
                if task_context.target_repo_git_root
                else "--skip-git-repo-check"
            ),
        }
        args = tuple(
            formatted
            for argument in self.codex_arguments
            if (formatted := argument.format(**values))
        )
        return (self.codex_executable, *args)

    def _collect_input_documents(
        self,
        invocation: AgentInvocation,
    ) -> tuple[tuple[AgentInputDocument, ...], tuple[str, ...]]:
        references = self._resolve_input_references(invocation)
        documents: list[AgentInputDocument] = []
        missing: list[str] = []

        seen_paths: set[str] = set()
        for reference in references:
            path = self._resolve_reference_to_path(reference)
            relative_path = self._workspace_relative(path)
            if relative_path in seen_paths:
                continue
            seen_paths.add(relative_path)
            if path.is_file():
                content = path.read_text(encoding="utf-8")
                documents.append(
                    AgentInputDocument(
                        reference=reference,
                        relative_path=relative_path,
                        content=content,
                    ),
                )
            else:
                missing.append(relative_path)
        return tuple(documents), tuple(missing)

    def _resolve_input_references(self, invocation: AgentInvocation) -> tuple[str, ...]:
        references: list[str] = [constants.TASK_FILENAME]
        if invocation.input_artifacts is None:
            references.extend(_ROLE_REQUIRED_ARTIFACTS.get(invocation.role, ()))
        else:
            references.extend(invocation.input_artifacts)

        include_all_current = (
            invocation.include_all_current_artifacts
            or invocation.role == constants.ROLE_PRODUCT_OWNER
        )
        if include_all_current:
            current_artifacts = self.artifact_store.list_current_artifacts()
            references.extend(current_artifacts.keys())

        normalized: list[str] = []
        for reference in references:
            normalized.append(_normalize_non_empty(reference, "input reference"))
        return tuple(normalized)

    def _resolve_reference_to_path(self, reference: str) -> Path:
        normalized = _normalize_non_empty(reference, "input reference")
        if normalized == constants.TASK_FILENAME:
            return self.task_workspace_path / constants.TASK_FILENAME
        if normalized in constants.ARTIFACT_TYPES:
            return self.artifact_store.current_artifact_path(normalized)

        candidate = Path(normalized).expanduser()
        if candidate.is_absolute():
            return candidate
        return (self.task_workspace_path / candidate).resolve()

    def _render_product_owner_contract_guardrails(self, task_context: object) -> list[str]:
        current_artifacts = getattr(task_context, "current_artifacts", {})
        allowed_artifact_types = "-"
        if isinstance(current_artifacts, Mapping) and current_artifacts:
            allowed_artifact_types = ", ".join(sorted(str(name) for name in current_artifacts))
        return [
            "## Product Owner Contract Guardrails",
            "",
            (
                "- `task.md` is a required system input. Read it, but never include `task` in "
                "`- based_on_artifacts`."
            ),
            (
                "- `event_log.md` is system context only when available. Never include "
                "`event_log` in `- based_on_artifacts`."
            ),
            (
                "- `- based_on_artifacts` may only list artifact types that already exist in "
                "`Current Artifacts`."
            ),
            f"- Allowed `based_on_artifacts` values for this run: {allowed_artifact_types}",
            "",
        ]

    def _render_prompt(
        self,
        *,
        invocation: AgentInvocation,
        task_context: object,
        role_prompt: str,
        prompt_asset_path: Path,
        input_documents: tuple[AgentInputDocument, ...],
        missing_input_artifacts: tuple[str, ...],
    ) -> str:
        context_items = sorted(invocation.extra_context.items())
        lines: list[str] = [
            "# xclaw Agent Invocation",
            "",
            "## Runtime Metadata",
            "",
            f"- task_id: {task_context.task_id}",
            f"- stage: {invocation.stage.value}",
            f"- role: {invocation.role}",
            f"- target_repo_path: {task_context.target_repo_path}",
            f"- task_workspace_path: {task_context.task_workspace_path}",
            (
                f"- local_skills_dir: {self.skills_dir}"
                if self.skills_dir and self.skills_dir.is_dir()
                else "- local_skills_dir: -"
            ),
            "",
            "## Objective",
            "",
            invocation.objective,
            "",
            "## Execution Contract",
            "",
            "- Read only from `task.md` and `current/*.md` inputs attached below.",
            (
                "- Return your structured role output as the final assistant message; "
                "the adapter will persist it to this run's `response.md`."
            ),
            "- Do not create or edit files under `runs/` directly.",
            (
                "- Do not modify `task.md`, `event_log.md`, `current/`, or `history/` directly."
            ),
            "- The Product Owner gateway is the only component allowed to publish current artifacts.",
            (
                "- If task-specific reusable guidance exists, inspect relevant `SKILL.md` files "
                "under the local skills directory above before proceeding."
            ),
            "",
        ]

        if invocation.role == constants.ROLE_PRODUCT_OWNER:
            lines.extend(self._render_product_owner_contract_guardrails(task_context))

        if context_items:
            lines.extend(
                [
                    "## Extra Context",
                    "",
                ],
            )
            for key, value in context_items:
                lines.append(f"- {key}: {value}")
            lines.append("")

        lines.extend(
            [
                "## Role Prompt Asset",
                "",
                f"- source: {prompt_asset_path}",
                "",
                _render_fenced_markdown(role_prompt),
                "## Input Artifacts",
                "",
            ],
        )

        for document in input_documents:
            lines.extend(
                [
                    f"### {document.reference} ({document.relative_path})",
                    "",
                    _render_fenced_markdown(document.content),
                ],
            )

        if missing_input_artifacts:
            lines.extend(
                [
                    "## Missing Inputs",
                    "",
                    "The following inputs are missing in workspace and should be treated as unavailable:",
                ],
            )
            for artifact_path in missing_input_artifacts:
                lines.append(f"- {artifact_path}")
            lines.append("")

        return "\n".join(lines).rstrip() + "\n"

    def _build_response_text(
        self,
        *,
        stdout: str,
        stderr: str,
        failure_reason: str | None,
    ) -> str:
        normalized_stdout = stdout.strip()
        if failure_reason is None and normalized_stdout:
            return normalized_stdout + "\n"

        lines: list[str] = []
        if failure_reason is not None:
            lines.extend(
                [
                    "# Agent Invocation Failed",
                    "",
                    f"- reason: {failure_reason}",
                ],
            )
        if stderr.strip():
            if lines:
                lines.append("")
            lines.extend(
                [
                    "## stderr",
                    "",
                    _render_fenced_text(stderr),
                ],
            )

        if not lines:
            lines = [
                "# Agent Invocation Completed",
                "",
                "- runner produced no stdout/stderr output.",
            ]
        return "\n".join(lines).rstrip() + "\n"

    def _resolve_response_text(
        self,
        *,
        response_file_path: Path,
        stdout: str,
        stderr: str,
        failure_reason: str | None,
    ) -> str:
        if failure_reason is None and response_file_path.is_file():
            output_text = response_file_path.read_text(encoding="utf-8").strip()
            if output_text:
                return output_text + "\n"

        return self._build_response_text(
            stdout=stdout,
            stderr=stderr,
            failure_reason=failure_reason,
        )

    def _build_run_log_text(
        self,
        *,
        invocation: AgentInvocation,
        task_context: object,
        command: tuple[str, ...],
        exit_code: int,
        prompt_path: str,
        response_path: str,
        missing_input_artifacts: tuple[str, ...],
        failure_reason: str | None,
        stdout: str,
        stderr: str,
    ) -> str:
        timestamp = _utc_now_iso()
        lines: list[str] = [
            f"timestamp={timestamp}",
            f"task_id={task_context.task_id}",
            f"stage={invocation.stage.value}",
            f"role={invocation.role}",
            f"target_repo_path={task_context.target_repo_path}",
            f"workspace_path={task_context.task_workspace_path}",
            f"command={' '.join(command)}",
            f"exit_code={exit_code}",
            f"prompt_path={prompt_path}",
            f"response_path={response_path}",
        ]
        if missing_input_artifacts:
            lines.append(f"missing_inputs={','.join(missing_input_artifacts)}")
        if failure_reason is not None:
            lines.append(f"failure_reason={failure_reason}")

        lines.extend(
            [
                "",
                "[stdout]",
                stdout.rstrip(),
                "",
                "[stderr]",
                stderr.rstrip(),
                "",
            ],
        )
        return "\n".join(lines)

    def _derive_summary(
        self,
        *,
        role: str,
        response_text: str,
        failure_reason: str | None,
    ) -> str:
        if failure_reason is not None:
            return f"{role} invocation failed: {failure_reason}"

        for line in response_text.splitlines():
            normalized = line.strip()
            if not normalized:
                continue
            if normalized.startswith("#"):
                normalized = normalized.lstrip("#").strip()
            if normalized.startswith("- "):
                normalized = normalized[2:].strip()
            if normalized:
                return normalized[:240]
        return f"{role} invocation completed."

    def _build_warnings(
        self,
        *,
        missing_input_artifacts: tuple[str, ...],
        failure_reason: str | None,
        stderr: str,
    ) -> tuple[str, ...]:
        warnings: list[str] = []
        if missing_input_artifacts:
            warnings.append(
                "missing_inputs="
                + ",".join(missing_input_artifacts),
            )
        if failure_reason is not None:
            warnings.append(failure_reason)
        normalized_stderr = stderr.strip()
        if normalized_stderr:
            warnings.append(normalized_stderr.splitlines()[0][:240])
        return tuple(warnings)

    def _workspace_relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.task_workspace_path).as_posix()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _normalize_non_empty(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string.")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must be non-empty.")
    return normalized


def _normalize_role(role: str) -> str:
    normalized = _normalize_non_empty(role, "role")
    if normalized not in _ROLE_PROMPT_FILENAMES:
        supported = ", ".join(sorted(_ROLE_PROMPT_FILENAMES))
        raise UnsupportedRoleError(f"Unsupported role {normalized!r}. Supported: {supported}.")
    return normalized


def _coerce_bool(value: bool, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{field_name} must be bool.")
    return value


def _render_fenced_markdown(content: str) -> str:
    normalized = content.rstrip("\n")
    fence = "```"
    if "```" in normalized:
        fence = "````"
    return f"{fence}markdown\n{normalized}\n{fence}\n"


def _render_fenced_text(content: str) -> str:
    normalized = content.rstrip("\n")
    fence = "```"
    if "```" in normalized:
        fence = "````"
    return f"{fence}text\n{normalized}\n{fence}\n"


def _parse_run_sequence(run_directory_name: str) -> int:
    match = _SEQUENCE_PREFIX_RE.match(run_directory_name)
    if match is None:
        return 1
    return int(match.group("seq"))


__all__ = [
    "AgentAdapter",
    "AgentAdapterContractError",
    "AgentAdapterError",
    "AgentInputDocument",
    "AgentInvocation",
    "AgentInvocationResult",
    "PromptAssetError",
    "Runner",
    "RunnerResult",
    "SubprocessRunner",
    "UnsupportedRoleError",
]
