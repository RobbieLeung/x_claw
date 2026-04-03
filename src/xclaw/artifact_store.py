"""Artifact store with current/history management and sequence allocators."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
import shutil
from typing import Sequence

from . import protocol as constants
from .markdown import MarkdownDocument, read_markdown_file, render_markdown
from .protocol import Stage
from .workspace import task_required_sections

_SEQUENCE_PREFIX_RE = re.compile(r"^(?P<seq>[0-9]+)_.+$")


class ArtifactStoreError(RuntimeError):
    """Base error for artifact-store operations."""


class ArtifactStoreContractError(ArtifactStoreError):
    """Raised when workspace artifact files violate expected contracts."""


class CurrentArtifactNotFoundError(ArtifactStoreError):
    """Raised when `current/*.md` is requested but does not exist."""


class ArtifactDeletionForbiddenError(ArtifactStoreError):
    """Raised when callers try to delete current artifacts directly."""


@dataclass(frozen=True)
class ArtifactPublication:
    """Result of one artifact publish/update operation."""

    artifact_type: str
    version: int
    current_path: str
    archived_path: str | None
    supersedes: str | None


class ArtifactStore:
    """Manage current artifacts, history archiving, and run directories."""

    def __init__(self, task_workspace_path: str | Path) -> None:
        self.task_workspace_path = Path(task_workspace_path).expanduser().resolve()
        self.task_file_path = self.task_workspace_path / constants.TASK_FILENAME
        self.current_dir = self.task_workspace_path / constants.CURRENT_DIRNAME
        self.history_dir = self.task_workspace_path / constants.HISTORY_DIRNAME
        self.runs_dir = self.task_workspace_path / constants.RUNS_DIRNAME

        if not self.task_file_path.is_file():
            raise ArtifactStoreError(f"task.md is missing: {self.task_file_path}")
        for directory_path in (
            self.current_dir,
            self.history_dir,
            self.runs_dir,
        ):
            directory_path.mkdir(parents=True, exist_ok=True)

        task_document = read_markdown_file(
            self.task_file_path,
            required_sections=task_required_sections(),
        )
        front_matter = task_document.front_matter
        self.task_id = _normalize_non_empty(str(front_matter["task_id"]), "task_id")
        self.target_repo_path = _normalize_non_empty(
            str(front_matter["target_repo_path"]),
            "target_repo_path",
        )

    def publish_artifact(
        self,
        *,
        artifact_type: str,
        body: str,
        stage: Stage | str,
        producer: str,
        consumer: str,
        status: str,
        created_at: str | None = None,
    ) -> ArtifactPublication:
        """
        Publish an artifact into `current/`.

        When a current artifact already exists, the old file is archived to
        `history/<artifact_type>/` and the new file increments the version.
        """

        normalized_type = _normalize_artifact_type(artifact_type)
        normalized_stage = Stage(stage)
        normalized_producer = _normalize_actor(producer, field_name="producer")
        normalized_consumer = _normalize_actor(consumer, field_name="consumer")
        normalized_status = _normalize_non_empty(status, "status")
        current_path = self.current_dir / constants.ARTIFACT_FILENAMES[normalized_type]
        current_relative_path = self._workspace_relative_path(current_path)

        next_version = 1
        supersedes: str | None = None
        archived_path: Path | None = None

        if current_path.is_file():
            current_document = read_markdown_file(current_path)
            current_front_matter = current_document.front_matter
            existing_type = str(current_front_matter.get("artifact_type", "")).strip()
            if existing_type != normalized_type:
                raise ArtifactStoreContractError(
                    f"current artifact type mismatch at {current_path}: "
                    f"expected {normalized_type!r}, got {existing_type!r}.",
                )
            previous_version = _normalize_existing_version(
                current_front_matter.get("version"),
                artifact_path=current_path,
            )
            next_version = previous_version + 1
            supersedes = f"{current_relative_path}@v{previous_version}"

            history_artifact_dir = self.history_dir / normalized_type
            history_artifact_dir.mkdir(parents=True, exist_ok=True)
            archived_path = history_artifact_dir / f"{normalized_type}.v{previous_version}.md"
            if archived_path.exists():
                raise ArtifactStoreContractError(
                    f"history version already exists: {archived_path}",
                )

        front_matter = {
            "task_id": self.task_id,
            "artifact_type": normalized_type,
            "stage": normalized_stage.value,
            "producer": normalized_producer,
            "consumer": normalized_consumer,
            "status": normalized_status,
            "version": next_version,
            "created_at": created_at or _utc_now_iso(),
            "supersedes": supersedes,
            "target_repo_path": self.target_repo_path,
        }
        rendered = render_markdown(front_matter, body)

        temp_path = current_path.with_suffix(".tmp")
        if temp_path.exists():
            temp_path.unlink()
        temp_path.write_text(rendered, encoding="utf-8")
        try:
            if archived_path is not None:
                shutil.copy2(current_path, archived_path)
            temp_path.replace(current_path)
        finally:
            if temp_path.exists():
                temp_path.unlink()

        return ArtifactPublication(
            artifact_type=normalized_type,
            version=next_version,
            current_path=current_relative_path,
            archived_path=(
                self._workspace_relative_path(archived_path) if archived_path is not None else None
            ),
            supersedes=supersedes,
        )

    def read_current_artifact(
        self,
        artifact_type: str,
        *,
        required_sections: Sequence[str] | None = None,
    ) -> MarkdownDocument:
        """Read one current artifact by type."""

        current_path = self.current_artifact_path(artifact_type)
        if not current_path.is_file():
            raise CurrentArtifactNotFoundError(
                f"Current artifact does not exist: {current_path}",
            )

        document = read_markdown_file(current_path, required_sections=required_sections)
        normalized_type = _normalize_artifact_type(artifact_type)
        if document.front_matter["artifact_type"] != normalized_type:
            raise ArtifactStoreContractError(
                f"current artifact type mismatch at {current_path}: "
                f"{document.front_matter['artifact_type']!r}",
            )
        return document

    def current_artifact_path(self, artifact_type: str) -> Path:
        """Return absolute current artifact path for type."""

        normalized_type = _normalize_artifact_type(artifact_type)
        return self.current_dir / constants.ARTIFACT_FILENAMES[normalized_type]

    def list_current_artifacts(self) -> dict[str, str]:
        """List existing current artifacts as workspace-relative paths."""

        artifacts: dict[str, str] = {}
        for artifact_type in constants.ARTIFACT_TYPES:
            path = self.current_dir / constants.ARTIFACT_FILENAMES[artifact_type]
            if path.is_file():
                artifacts[artifact_type] = self._workspace_relative_path(path)
        return artifacts

    def delete_current_artifact(self, artifact_type: str) -> None:
        """
        Block direct current-artifact deletion.

        Callers must publish a new version through `publish_artifact`.
        """

        normalized_type = _normalize_artifact_type(artifact_type)
        raise ArtifactDeletionForbiddenError(
            "Deleting current artifacts is forbidden. "
            f"Publish a superseding version for {normalized_type!r} instead.",
        )

    def allocate_run_directory(self, *, role: str, create: bool = True) -> Path:
        """Allocate sequential run directory under `runs/`."""

        normalized_role = _normalize_actor(role, field_name="role")
        sequence = _next_sequence(self.runs_dir)
        run_dir = self.runs_dir / f"{sequence:04d}_{normalized_role}"
        if create:
            run_dir.mkdir(parents=False, exist_ok=False)
        return run_dir

    def _workspace_relative_path(self, path: Path) -> str:
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


def _normalize_artifact_type(artifact_type: str) -> str:
    normalized = _normalize_non_empty(artifact_type, "artifact_type")
    if normalized not in constants.ARTIFACT_TYPES:
        allowed = ", ".join(constants.ARTIFACT_TYPES)
        raise ValueError(f"artifact_type must be one of: {allowed}.")
    return normalized


def _normalize_actor(
    actor: str,
    *,
    field_name: str,
    allow_human: bool = True,
    allow_system: bool = False,
) -> str:
    normalized = _normalize_non_empty(actor, field_name)
    allowed = set(constants.ROLE_NAMES)
    if allow_human:
        allowed.add("human")
    if allow_system:
        allowed.add("system")
    if normalized not in allowed:
        allowed_display = ", ".join(sorted(allowed))
        raise ValueError(f"{field_name} must be one of: {allowed_display}.")
    return normalized


def _normalize_existing_version(version: object, *, artifact_path: Path) -> int:
    if isinstance(version, int):
        parsed = version
    elif isinstance(version, str) and version.strip().isdigit():
        parsed = int(version.strip())
    else:
        raise ArtifactStoreContractError(
            f"Invalid version in {artifact_path}: {version!r}",
        )
    if parsed < 1:
        raise ArtifactStoreContractError(
            f"Version in {artifact_path} must be >= 1, got {parsed}.",
        )
    return parsed


def _next_sequence(directory: Path) -> int:
    max_sequence = 0
    for entry in directory.iterdir():
        if not entry.is_dir():
            continue
        match = _SEQUENCE_PREFIX_RE.match(entry.name)
        if match is None:
            continue
        max_sequence = max(max_sequence, int(match.group("seq")))
    return max_sequence + 1


__all__ = [
    "ArtifactDeletionForbiddenError",
    "ArtifactPublication",
    "ArtifactStore",
    "ArtifactStoreContractError",
    "ArtifactStoreError",
    "CurrentArtifactNotFoundError",
]
