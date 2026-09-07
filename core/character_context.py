"""Pack-driven optional narrative context for a character.

This is deliberately separate from deterministic character mechanics. A rulepack may
ship ``rulepacks/data/<system>/character_context.yaml`` to ask a few structured
questions after mechanical creation (current status, allegiance, personal goal, campaign
premise, etc.). Core knows only generic field shapes and persists canonical values on the
sheet; it never knows what an Inquisitor, clan, faction, or setting-specific status is.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from core.yaml_safety import safe_load_no_aliases

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BUILTIN_DATA_ROOT = _REPO_ROOT / "rulepacks" / "data"
_STATE_KEY = "__character_context__"
_ALLOWED_FIELD_KINDS = frozenset({"choice", "text", "textarea"})
_MAX_TEXT = 2000


class CharacterContextError(ValueError):
    """A character-context declaration or submitted value is invalid."""


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _candidate_sidecars(pack: Any, data_root: Path | None = None) -> list[Path]:
    system = str(getattr(pack, "system", "")).strip()
    if not system:
        raise CharacterContextError("pack has no system id")  # i18n-exempt: internal validation
    if any(part in system for part in ("/", "\\", "..")):
        raise CharacterContextError(
            "pack system id is not safe for a character-context path"  # i18n-exempt: internal validation
        )
    if data_root is not None:
        return [Path(data_root) / system / "character_context.yaml"]

    candidates: list[Path] = []
    try:
        import core.rulepacks as rulepacks

        user_root = getattr(rulepacks, "_USER_RULEPACK_DIR", None)
    except Exception:  # pragma: no cover - defensive import boundary
        user_root = None
    if user_root is not None:
        candidates.append(Path(user_root) / "data" / system / "character_context.yaml")
    candidates.append(_BUILTIN_DATA_ROOT / system / "character_context.yaml")
    return candidates


def load_character_context_spec(pack: Any, *, data_root: Path | None = None) -> Mapping[str, Any] | None:
    """Load and strictly validate the pack's optional narrative-context declaration."""

    path = next((candidate for candidate in _candidate_sidecars(pack, data_root) if candidate.is_file()), None)
    if path is None:
        return None
    try:
        raw = safe_load_no_aliases(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise CharacterContextError(
            f"could not load character-context sidecar {path.name!r}: {exc}"  # i18n-exempt: internal validation
        ) from exc
    if not isinstance(raw, Mapping):
        raise CharacterContextError(
            "character-context sidecar root must be a mapping"  # i18n-exempt: internal validation
        )
    if int(raw.get("version", 1)) != 1:
        raise CharacterContextError(
            "unsupported character-context sidecar version"  # i18n-exempt: internal validation
        )
    unknown = set(raw) - {"version", "optional", "fields"}
    if unknown:
        raise CharacterContextError(
            f"character-context sidecar has unknown root keys {sorted(unknown)}"  # i18n-exempt: internal validation
        )
    optional = raw.get("optional", True)
    if not isinstance(optional, bool):
        raise CharacterContextError(
            "character-context optional must be boolean"  # i18n-exempt: internal validation
        )
    fields = raw.get("fields") or {}
    if not isinstance(fields, Mapping) or not fields:
        raise CharacterContextError(
            "character-context fields must be a non-empty mapping"  # i18n-exempt: internal validation
        )

    seen: set[str] = set()
    for field_id_raw, field_raw in fields.items():
        field_id = str(field_id_raw).strip()
        if not field_id or field_id in seen or not isinstance(field_raw, Mapping):
            raise CharacterContextError(
                "character-context contains an invalid field"  # i18n-exempt: internal validation
            )
        seen.add(field_id)
        extra = set(field_raw) - {"kind", "required", "display", "options", "placeholder"}
        if extra:
            raise CharacterContextError(
                f"character-context field {field_id!r} has unknown keys {sorted(extra)}"  # i18n-exempt: internal validation
            )
        kind = str(field_raw.get("kind") or "text").strip().casefold()
        if kind not in _ALLOWED_FIELD_KINDS:
            raise CharacterContextError(
                f"character-context field {field_id!r}.kind must be one of {sorted(_ALLOWED_FIELD_KINDS)}"  # i18n-exempt: internal validation
            )
        required = field_raw.get("required", False)
        if not isinstance(required, bool):
            raise CharacterContextError(
                f"character-context field {field_id!r}.required must be boolean"  # i18n-exempt: internal validation
            )
        options = field_raw.get("options") or {}
        if kind == "choice":
            if not isinstance(options, Mapping) or not options:
                raise CharacterContextError(
                    f"character-context choice {field_id!r} requires options"  # i18n-exempt: internal validation
                )
            for option_id_raw, option_raw in options.items():
                option_id = str(option_id_raw).strip()
                if not option_id or not isinstance(option_raw, Mapping):
                    raise CharacterContextError(
                        f"character-context choice {field_id!r} has invalid option"  # i18n-exempt: internal validation
                    )
                option_extra = set(option_raw) - {"display"}
                if option_extra:
                    raise CharacterContextError(
                        f"character-context choice {field_id!r}.{option_id!r} has unknown keys {sorted(option_extra)}"  # i18n-exempt: internal validation
                    )
        elif options:
            raise CharacterContextError(
                f"character-context field {field_id!r} may use options only when kind=choice"  # i18n-exempt: internal validation
            )
    return raw


def _localized(raw: Mapping[str, Any], locale: str, fallback: str, key: str = "display") -> str:
    display = _mapping(raw.get(key))
    locale_key = str(locale or "en").casefold()
    language = locale_key.split("-", 1)[0].split("_", 1)[0]
    for candidate in (locale_key, language, "en"):
        value = display.get(candidate)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return fallback


def _state(character: Any) -> Mapping[str, Any] | None:
    secondary = getattr(character, "secondary_attributes", None)
    if not isinstance(secondary, dict):
        raise CharacterContextError(
            "character secondary attribute storage must be a mapping"  # i18n-exempt: internal validation
        )
    raw = secondary.get(_STATE_KEY)
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise CharacterContextError(
            "character-context state must be a mapping"  # i18n-exempt: internal validation
        )
    if raw.get("version") != 1:
        raise CharacterContextError(
            "unsupported character-context state version"  # i18n-exempt: internal validation
        )
    values = raw.get("values") or {}
    if not isinstance(values, Mapping):
        raise CharacterContextError(
            "character-context values must be a mapping"  # i18n-exempt: internal validation
        )
    return raw


def character_context_values(character: Any) -> dict[str, str]:
    """Return persisted canonical context values, excluding completion bookkeeping."""

    raw = _state(character)
    if raw is None:
        return {}
    values = _mapping(raw.get("values"))
    return {str(key): str(value) for key, value in values.items() if isinstance(value, str)}


def character_context_surface(pack: Any, character: Any, locale: str) -> dict[str, Any] | None:
    """Project the optional context form and its persisted values for a rich client."""

    spec = load_character_context_spec(pack)
    if spec is None:
        return None
    raw_state = _state(character)
    values = character_context_values(character)
    fields_wire: list[dict[str, Any]] = []
    for field_id_raw, field_raw_any in _mapping(spec.get("fields")).items():
        if not isinstance(field_raw_any, Mapping):
            continue
        field_id = str(field_id_raw)
        kind = str(field_raw_any.get("kind") or "text").casefold()
        row: dict[str, Any] = {
            "id": field_id,
            "kind": kind,
            "required": bool(field_raw_any.get("required", False)),
            "label": _localized(field_raw_any, locale, field_id),
        }
        placeholder = _localized(field_raw_any, locale, "", key="placeholder")
        if placeholder:
            row["placeholder"] = placeholder
        if kind == "choice":
            row["options"] = [
                {
                    "id": str(option_id),
                    "label": _localized(option_raw, locale, str(option_id)),
                }
                for option_id, option_raw in _mapping(field_raw_any.get("options")).items()
                if isinstance(option_raw, Mapping)
            ]
        fields_wire.append(row)
    return {
        "available": True,
        "optional": bool(spec.get("optional", True)),
        "complete": bool(raw_state and raw_state.get("complete") is True),
        "skipped": bool(raw_state and raw_state.get("skipped") is True),
        "fields": fields_wire,
        "values": values,
    }


def set_character_context(
    pack: Any,
    character: Any,
    values: Mapping[str, Any] | None = None,
    *,
    skip: bool = False,
) -> dict[str, str]:
    """Validate and persist one optional narrative-context form atomically on the sheet."""

    spec = load_character_context_spec(pack)
    if spec is None:
        raise CharacterContextError(
            "rulepack has no character-context sidecar"  # i18n-exempt: internal validation
        )
    secondary = getattr(character, "secondary_attributes", None)
    if not isinstance(secondary, dict):
        raise CharacterContextError(
            "character secondary attribute storage must be a mapping"  # i18n-exempt: internal validation
        )
    if skip:
        if not bool(spec.get("optional", True)):
            raise CharacterContextError(
                "character-context form is not optional"  # i18n-exempt: internal validation
            )
        secondary[_STATE_KEY] = {"version": 1, "complete": True, "skipped": True, "values": {}}
        return {}

    submitted = dict(values or {})
    fields = _mapping(spec.get("fields"))
    unknown = set(submitted) - {str(field_id) for field_id in fields}
    if unknown:
        raise CharacterContextError(
            f"unknown character-context fields: {sorted(unknown)}"  # i18n-exempt: internal validation
        )

    cleaned: dict[str, str] = {}
    any_value = False
    for field_id_raw, field_raw_any in fields.items():
        field_id = str(field_id_raw)
        if not isinstance(field_raw_any, Mapping):
            continue
        value_raw = submitted.get(field_id, "")
        if value_raw is None:
            value_raw = ""
        if not isinstance(value_raw, str):
            raise CharacterContextError(
                f"character-context field {field_id!r} must be text"  # i18n-exempt: internal validation
            )
        value = value_raw.strip()
        if len(value) > _MAX_TEXT:
            raise CharacterContextError(
                f"character-context field {field_id!r} is too long"  # i18n-exempt: internal validation
            )
        if value:
            any_value = True
        kind = str(field_raw_any.get("kind") or "text").casefold()
        if kind == "choice" and value:
            options = _mapping(field_raw_any.get("options"))
            if value not in {str(option_id) for option_id in options}:
                raise CharacterContextError(
                    f"invalid character-context choice for {field_id!r}"  # i18n-exempt: internal validation
                )
        cleaned[field_id] = value

    if not any_value and bool(spec.get("optional", True)):
        secondary[_STATE_KEY] = {"version": 1, "complete": True, "skipped": True, "values": {}}
        return {}

    missing = [
        str(field_id)
        for field_id, field_raw in fields.items()
        if isinstance(field_raw, Mapping)
        and bool(field_raw.get("required", False))
        and not cleaned.get(str(field_id), "")
    ]
    if missing:
        raise CharacterContextError(
            f"required character-context fields are missing: {', '.join(missing)}"  # i18n-exempt: internal validation
        )

    secondary[_STATE_KEY] = {
        "version": 1,
        "complete": True,
        "skipped": False,
        "values": cleaned,
    }
    return cleaned
