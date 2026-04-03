"""Markdown front matter parsing, rendering, and contract validation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import re
from typing import Mapping, Sequence, Union

from . import protocol as constants
from .protocol import Stage

FRONT_MATTER_DELIMITER = "---"

FIELD_TASK_ID = "task_id"
FIELD_ARTIFACT_TYPE = "artifact_type"
FIELD_STAGE = "stage"
FIELD_PRODUCER = "producer"
FIELD_CONSUMER = "consumer"
FIELD_STATUS = "status"
FIELD_VERSION = "version"
FIELD_CREATED_AT = "created_at"
FIELD_SUPERSEDES = "supersedes"
FIELD_TARGET_REPO_PATH = constants.FIELD_TARGET_REPO_PATH

FRONT_MATTER_REQUIRED_FIELDS: tuple[str, ...] = (
    FIELD_TASK_ID,
    FIELD_ARTIFACT_TYPE,
    FIELD_STAGE,
    FIELD_PRODUCER,
    FIELD_CONSUMER,
    FIELD_STATUS,
    FIELD_VERSION,
    FIELD_CREATED_AT,
    FIELD_SUPERSEDES,
    FIELD_TARGET_REPO_PATH,
)

# v1 defaults to strict field names to avoid contract drift.
FRONT_MATTER_ALLOWED_FIELDS: tuple[str, ...] = FRONT_MATTER_REQUIRED_FIELDS

SYSTEM_ARTIFACT_TYPES: tuple[str, ...] = ("task", "event_log")
ALLOWED_ARTIFACT_TYPES: tuple[str, ...] = constants.ARTIFACT_TYPES + SYSTEM_ARTIFACT_TYPES
ALLOWED_STAGES: tuple[str, ...] = tuple(stage.value for stage in Stage)
ALLOWED_ROLES: tuple[str, ...] = constants.ROLE_NAMES + ("human", "system")

_ARTIFACT_STATUS_VALUES: tuple[str, ...] = (
    "draft",
    "in_progress",
    "pending",
    "ready",
    "blocked",
    "needs_repair",
    "superseded",
    "final",
)
ALLOWED_STATUSES: tuple[str, ...] = constants.TASK_STATUS_NAMES + _ARTIFACT_STATUS_VALUES

FrontMatterValue = Union[str, int, bool, None]
FrontMatter = dict[str, FrontMatterValue]

_PLAIN_STRING_RE = re.compile(r"^[A-Za-z0-9_.:/@+\\-]+$")
_WINDOWS_ABS_PATH_RE = re.compile(r"^[A-Za-z]:[\\\\/].+")
_INT_RE = re.compile(r"^[+-]?[0-9]+$")
_MARKDOWN_HEADING_RE = re.compile(r"^#{1,6}\s+\S+", re.MULTILINE)


class MarkdownContractError(ValueError):
    """Base class for markdown contract errors."""


class FrontMatterParseError(MarkdownContractError):
    """Raised when raw markdown front matter cannot be parsed."""


class FrontMatterValidationError(MarkdownContractError):
    """Raised when front matter violates the xclaw contract."""


class MarkdownBodyValidationError(MarkdownContractError):
    """Raised when markdown body lacks required structure."""


@dataclass(frozen=True)
class FrontMatterSchema:
    """Validation schema for xclaw markdown front matter."""

    required_fields: tuple[str, ...] = FRONT_MATTER_REQUIRED_FIELDS
    allowed_fields: tuple[str, ...] = FRONT_MATTER_ALLOWED_FIELDS
    allowed_artifact_types: tuple[str, ...] = ALLOWED_ARTIFACT_TYPES
    allowed_stages: tuple[str, ...] = ALLOWED_STAGES
    allowed_producers: tuple[str, ...] = ALLOWED_ROLES
    allowed_consumers: tuple[str, ...] = ALLOWED_ROLES
    allowed_statuses: tuple[str, ...] = ALLOWED_STATUSES

    def __post_init__(self) -> None:
        required = set(self.required_fields)
        allowed = set(self.allowed_fields)
        if not required.issubset(allowed):
            missing = ", ".join(sorted(required - allowed))
            raise ValueError(
                f"FrontMatterSchema.allowed_fields must include all required fields: {missing}.",
            )


DEFAULT_FRONT_MATTER_SCHEMA = FrontMatterSchema()


@dataclass
class MarkdownDocument:
    """Structured markdown document composed of front matter and body."""

    front_matter: FrontMatter
    body: str

    def to_text(
        self,
        *,
        schema: FrontMatterSchema | None = None,
        required_sections: Sequence[str] | None = None,
    ) -> str:
        return render_markdown(
            self.front_matter,
            self.body,
            schema=schema,
            required_sections=required_sections,
        )


def parse_markdown_text(
    text: str,
    *,
    schema: FrontMatterSchema | None = None,
    required_sections: Sequence[str] | None = None,
) -> MarkdownDocument:
    """Parse markdown text and validate both front matter and body."""

    front_matter, body = parse_front_matter(text)
    validated_front_matter = validate_front_matter(
        front_matter,
        schema=schema,
    )
    validate_markdown_body(body, required_sections=required_sections)
    return MarkdownDocument(front_matter=validated_front_matter, body=body)


def read_markdown_file(
    path: str | Path,
    *,
    schema: FrontMatterSchema | None = None,
    required_sections: Sequence[str] | None = None,
) -> MarkdownDocument:
    """Read and parse a markdown file from disk."""

    document_path = Path(path)
    text = document_path.read_text(encoding="utf-8")
    return parse_markdown_text(
        text,
        schema=schema,
        required_sections=required_sections,
    )


def write_markdown_file(
    path: str | Path,
    *,
    front_matter: Mapping[str, FrontMatterValue],
    body: str,
    schema: FrontMatterSchema | None = None,
    required_sections: Sequence[str] | None = None,
    create_parent: bool = True,
) -> MarkdownDocument:
    """Validate and write a markdown document to disk."""

    normalized_front_matter = validate_front_matter(front_matter, schema=schema)
    validate_markdown_body(body, required_sections=required_sections)
    rendered = render_markdown(
        normalized_front_matter,
        body,
        schema=schema,
        required_sections=required_sections,
    )

    document_path = Path(path)
    if create_parent:
        document_path.parent.mkdir(parents=True, exist_ok=True)
    document_path.write_text(rendered, encoding="utf-8")
    return MarkdownDocument(front_matter=normalized_front_matter, body=body)


def update_markdown_file(
    path: str | Path,
    *,
    front_matter_updates: Mapping[str, FrontMatterValue] | None = None,
    body: str | None = None,
    schema: FrontMatterSchema | None = None,
    required_sections: Sequence[str] | None = None,
) -> MarkdownDocument:
    """Update front matter and/or body of a markdown file with full validation."""

    existing = read_markdown_file(path, schema=schema, required_sections=required_sections)
    merged_front_matter: FrontMatter = dict(existing.front_matter)
    if front_matter_updates:
        for key, value in front_matter_updates.items():
            merged_front_matter[str(key)] = value

    next_body = existing.body if body is None else body
    return write_markdown_file(
        path,
        front_matter=merged_front_matter,
        body=next_body,
        schema=schema,
        required_sections=required_sections,
        create_parent=False,
    )


def render_markdown(
    front_matter: Mapping[str, FrontMatterValue],
    body: str,
    *,
    schema: FrontMatterSchema | None = None,
    required_sections: Sequence[str] | None = None,
) -> str:
    """Render validated front matter + body into a markdown string."""

    schema_obj = schema or DEFAULT_FRONT_MATTER_SCHEMA
    normalized_front_matter = validate_front_matter(front_matter, schema=schema_obj)
    validate_markdown_body(body, required_sections=required_sections)

    lines: list[str] = [FRONT_MATTER_DELIMITER]
    for key in _ordered_front_matter_keys(normalized_front_matter, schema_obj):
        lines.append(f"{key}: {_format_front_matter_scalar(normalized_front_matter[key])}")
    lines.append(FRONT_MATTER_DELIMITER)

    normalized_body = body
    if normalized_body and not normalized_body.endswith("\n"):
        normalized_body = f"{normalized_body}\n"
    return "\n".join(lines) + "\n" + normalized_body


def parse_front_matter(text: str) -> tuple[FrontMatter, str]:
    """Split markdown into parsed front matter dictionary and body."""

    if not isinstance(text, str):
        raise TypeError("text must be a string.")
    if not text:
        raise FrontMatterParseError("Markdown content is empty.")

    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != FRONT_MATTER_DELIMITER:
        raise FrontMatterParseError(
            "Markdown must start with front matter delimiter '---'.",
        )

    closing_index = -1
    for index in range(1, len(lines)):
        if lines[index].strip() == FRONT_MATTER_DELIMITER:
            closing_index = index
            break
    if closing_index == -1:
        raise FrontMatterParseError("Front matter closing delimiter '---' is missing.")

    front_matter_block = "".join(lines[1:closing_index])
    body = "".join(lines[closing_index + 1 :])
    parsed_front_matter = _parse_front_matter_block(front_matter_block)
    return parsed_front_matter, body


def validate_front_matter(
    front_matter: Mapping[str, FrontMatterValue],
    *,
    schema: FrontMatterSchema | None = None,
) -> FrontMatter:
    """Validate front matter against schema and return normalized values."""

    if not isinstance(front_matter, Mapping):
        raise TypeError("front_matter must be a mapping.")

    schema_obj = schema or DEFAULT_FRONT_MATTER_SCHEMA
    normalized: FrontMatter = {}
    for raw_key, raw_value in front_matter.items():
        if not isinstance(raw_key, str):
            raise FrontMatterValidationError("Front matter keys must be strings.")
        key = raw_key.strip()
        if not key:
            raise FrontMatterValidationError("Front matter keys must be non-empty.")
        if key != raw_key:
            raise FrontMatterValidationError(
                f"Front matter key {raw_key!r} contains leading/trailing spaces.",
            )
        if key in normalized:
            raise FrontMatterValidationError(f"Duplicate front matter key: {key}.")
        normalized[key] = _normalize_front_matter_value(raw_value)

    missing_fields = [field for field in schema_obj.required_fields if field not in normalized]
    if missing_fields:
        missing_display = ", ".join(missing_fields)
        raise FrontMatterValidationError(
            f"Missing required front matter field(s): {missing_display}.",
        )

    unknown_fields = [field for field in normalized if field not in schema_obj.allowed_fields]
    if unknown_fields:
        details = []
        for field in unknown_fields:
            hint = _case_sensitive_field_hint(field, schema_obj.allowed_fields)
            if hint:
                details.append(f"{field} (did you mean {hint}?)")
            else:
                details.append(field)
        drift_display = ", ".join(details)
        raise FrontMatterValidationError(
            f"Unknown front matter field(s): {drift_display}.",
        )

    _validate_membership(
        normalized,
        field_name=FIELD_ARTIFACT_TYPE,
        allowed_values=schema_obj.allowed_artifact_types,
    )
    _validate_membership(
        normalized,
        field_name=FIELD_STAGE,
        allowed_values=schema_obj.allowed_stages,
    )
    _validate_membership(
        normalized,
        field_name=FIELD_PRODUCER,
        allowed_values=schema_obj.allowed_producers,
    )
    _validate_membership(
        normalized,
        field_name=FIELD_CONSUMER,
        allowed_values=schema_obj.allowed_consumers,
    )
    _validate_membership(
        normalized,
        field_name=FIELD_STATUS,
        allowed_values=schema_obj.allowed_statuses,
    )

    normalized[FIELD_TASK_ID] = _coerce_non_empty_string(
        normalized[FIELD_TASK_ID],
        FIELD_TASK_ID,
    )
    normalized[FIELD_VERSION] = _coerce_positive_int(
        normalized[FIELD_VERSION],
        FIELD_VERSION,
    )
    normalized[FIELD_CREATED_AT] = _coerce_iso_timestamp(
        normalized[FIELD_CREATED_AT],
        FIELD_CREATED_AT,
    )
    normalized[FIELD_TARGET_REPO_PATH] = _coerce_absolute_path(
        normalized[FIELD_TARGET_REPO_PATH],
        FIELD_TARGET_REPO_PATH,
    )
    normalized[FIELD_SUPERSEDES] = _coerce_optional_non_empty_string(
        normalized[FIELD_SUPERSEDES],
        FIELD_SUPERSEDES,
    )

    return normalized


def validate_markdown_body(
    body: str,
    *,
    required_sections: Sequence[str] | None = None,
) -> None:
    """Validate markdown body has minimum structure and required sections."""

    if not isinstance(body, str):
        raise TypeError("body must be a string.")
    if not body.strip():
        raise MarkdownBodyValidationError("Markdown body is empty.")
    if _MARKDOWN_HEADING_RE.search(body) is None:
        raise MarkdownBodyValidationError(
            "Markdown body must include at least one heading line.",
        )

    if required_sections:
        missing_sections = [
            section
            for section in required_sections
            if not _contains_heading(body, section)
        ]
        if missing_sections:
            missing_display = ", ".join(missing_sections)
            raise MarkdownBodyValidationError(
                f"Markdown body missing required section heading(s): {missing_display}.",
            )


def _parse_front_matter_block(block: str) -> FrontMatter:
    front_matter: FrontMatter = {}
    for line_no, raw_line in enumerate(block.splitlines(), start=2):
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            continue
        if ":" not in raw_line:
            raise FrontMatterParseError(
                f"Invalid front matter line {line_no}: missing ':' separator.",
            )
        raw_key, raw_value = raw_line.split(":", 1)
        key = raw_key.strip()
        if not key:
            raise FrontMatterParseError(
                f"Invalid front matter line {line_no}: empty field name.",
            )
        if key in front_matter:
            raise FrontMatterParseError(f"Duplicate front matter field: {key}.")
        front_matter[key] = _parse_front_matter_scalar(
            raw_value.strip(),
            line_no=line_no,
            field_name=key,
        )
    return front_matter


def _parse_front_matter_scalar(
    raw_value: str,
    *,
    line_no: int,
    field_name: str,
) -> FrontMatterValue:
    if raw_value == "":
        return ""

    lowered = raw_value.lower()
    if lowered in {"null", "~", "none"}:
        return None
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if _INT_RE.fullmatch(raw_value):
        return int(raw_value)
    if raw_value.startswith('"') and raw_value.endswith('"'):
        try:
            parsed = json.loads(raw_value)
        except json.JSONDecodeError as exc:
            raise FrontMatterParseError(
                f"Invalid quoted value for field {field_name!r} at line {line_no}.",
            ) from exc
        if not isinstance(parsed, str):
            raise FrontMatterParseError(
                f"Quoted value for field {field_name!r} at line {line_no} must be a string.",
            )
        return parsed
    if raw_value.startswith("'") and raw_value.endswith("'"):
        return raw_value[1:-1]
    if raw_value.startswith(("'", '"')) and not raw_value.endswith(raw_value[0]):
        raise FrontMatterParseError(
            f"Unclosed quoted value for field {field_name!r} at line {line_no}.",
        )
    return raw_value


def _normalize_front_matter_value(value: FrontMatterValue) -> FrontMatterValue:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return value
    raise FrontMatterValidationError(
        "Front matter values must be str, int, bool, or null.",
    )


def _validate_membership(
    front_matter: Mapping[str, FrontMatterValue],
    *,
    field_name: str,
    allowed_values: Sequence[str],
) -> None:
    value = _coerce_non_empty_string(front_matter[field_name], field_name)
    if value not in allowed_values:
        allowed_display = ", ".join(allowed_values)
        raise FrontMatterValidationError(
            f"{field_name} must be one of: {allowed_display}.",
        )
    if isinstance(front_matter, dict):
        front_matter[field_name] = value


def _coerce_non_empty_string(value: FrontMatterValue, field_name: str) -> str:
    if not isinstance(value, str):
        raise FrontMatterValidationError(f"{field_name} must be a non-empty string.")
    stripped = value.strip()
    if not stripped:
        raise FrontMatterValidationError(f"{field_name} must be a non-empty string.")
    return stripped


def _coerce_optional_non_empty_string(
    value: FrontMatterValue,
    field_name: str,
) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        return stripped
    raise FrontMatterValidationError(f"{field_name} must be a string or null.")


def _coerce_positive_int(value: FrontMatterValue, field_name: str) -> int:
    if isinstance(value, bool):
        raise FrontMatterValidationError(f"{field_name} must be a positive integer.")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and _INT_RE.fullmatch(value.strip()):
        parsed = int(value.strip())
    else:
        raise FrontMatterValidationError(f"{field_name} must be a positive integer.")
    if parsed < 1:
        raise FrontMatterValidationError(f"{field_name} must be >= 1.")
    return parsed


def _coerce_iso_timestamp(value: FrontMatterValue, field_name: str) -> str:
    timestamp = _coerce_non_empty_string(value, field_name)
    normalized = timestamp.replace("Z", "+00:00")
    try:
        datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise FrontMatterValidationError(
            f"{field_name} must be a valid ISO-8601 timestamp.",
        ) from exc
    return timestamp


def _coerce_absolute_path(value: FrontMatterValue, field_name: str) -> str:
    path_value = _coerce_non_empty_string(value, field_name)
    is_posix_absolute = path_value.startswith("/")
    is_windows_absolute = _WINDOWS_ABS_PATH_RE.match(path_value) is not None
    if not (is_posix_absolute or is_windows_absolute):
        raise FrontMatterValidationError(f"{field_name} must be an absolute path.")
    return path_value


def _ordered_front_matter_keys(
    front_matter: Mapping[str, FrontMatterValue],
    schema: FrontMatterSchema,
) -> list[str]:
    ordered: list[str] = []
    for field in schema.required_fields:
        if field in front_matter:
            ordered.append(field)
    for field in front_matter:
        if field not in ordered:
            ordered.append(field)
    return ordered


def _format_front_matter_scalar(value: FrontMatterValue) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if not isinstance(value, str):
        raise TypeError("Front matter scalar must be str, int, bool, or None.")
    if _can_emit_plain_string(value):
        return value
    return json.dumps(value, ensure_ascii=True)


def _can_emit_plain_string(value: str) -> bool:
    if not value:
        return False
    lowered = value.lower()
    if lowered in {"null", "none", "~", "true", "false"}:
        return False
    if _INT_RE.fullmatch(value):
        return False
    return _PLAIN_STRING_RE.fullmatch(value) is not None


def _contains_heading(body: str, section_name: str) -> bool:
    escaped = re.escape(section_name.strip())
    if not escaped:
        return False
    pattern = re.compile(rf"^#{{1,6}}\s+{escaped}\s*$", re.MULTILINE)
    return pattern.search(body) is not None


def _case_sensitive_field_hint(field_name: str, allowed_fields: Sequence[str]) -> str | None:
    lowered = field_name.lower()
    for candidate in allowed_fields:
        if candidate.lower() == lowered:
            return candidate
    return None


__all__ = [
    "ALLOWED_ARTIFACT_TYPES",
    "ALLOWED_ROLES",
    "ALLOWED_STAGES",
    "ALLOWED_STATUSES",
    "DEFAULT_FRONT_MATTER_SCHEMA",
    "FRONT_MATTER_ALLOWED_FIELDS",
    "FRONT_MATTER_DELIMITER",
    "FRONT_MATTER_REQUIRED_FIELDS",
    "FrontMatter",
    "FrontMatterParseError",
    "FrontMatterSchema",
    "FrontMatterValidationError",
    "FrontMatterValue",
    "MarkdownBodyValidationError",
    "MarkdownContractError",
    "MarkdownDocument",
    "parse_front_matter",
    "parse_markdown_text",
    "read_markdown_file",
    "render_markdown",
    "update_markdown_file",
    "validate_front_matter",
    "validate_markdown_body",
    "write_markdown_file",
]
