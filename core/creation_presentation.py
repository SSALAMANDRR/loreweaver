"""Presentation-only metadata for staged character creation.

A rulepack may ship ``rulepacks/data/<system>/creation_presentation.yaml`` to
explain creation stages and label otherwise-canonical UI tokens.  This module
never changes character mechanics; it only localizes authored strings for rich
clients.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from core.yaml_safety import safe_load_no_aliases

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BUILTIN_DATA_ROOT = _REPO_ROOT / "rulepacks" / "data"
_ALLOWED_SECTIONS = frozenset({"stages", "choice_groups", "advancement_stages", "advancement_categories"})
_ALLOWED_STAGE_KEYS = frozenset({"title", "description", "choice", "effect"})


class CreationPresentationError(ValueError):
    """A creation-presentation sidecar is malformed."""


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _candidate_sidecars(pack: Any, data_root: Path | None = None) -> list[Path]:
    system = str(getattr(pack, "system", "")).strip()
    if not system:
        raise CreationPresentationError("pack has no system id")
    if any(part in system for part in ("/", "\\", "..")):
        raise CreationPresentationError("pack system id is not safe for a presentation path")
    if data_root is not None:
        return [Path(data_root) / system / "creation_presentation.yaml"]

    candidates: list[Path] = []
    try:
        import core.rulepacks as rulepacks

        user_root = getattr(rulepacks, "_USER_RULEPACK_DIR", None)
    except Exception:  # pragma: no cover - defensive import boundary
        user_root = None
    if user_root is not None:
        candidates.append(Path(user_root) / "data" / system / "creation_presentation.yaml")
    candidates.append(_BUILTIN_DATA_ROOT / system / "creation_presentation.yaml")
    return candidates


def load_creation_presentation(pack: Any, *, data_root: Path | None = None) -> Mapping[str, Any]:
    path = next((candidate for candidate in _candidate_sidecars(pack, data_root) if candidate.is_file()), None)
    if path is None:
        return {}
    try:
        raw = safe_load_no_aliases(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise CreationPresentationError(f"could not load creation presentation {path.name!r}: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise CreationPresentationError("creation presentation root must be a mapping")
    if int(raw.get("version", 1)) != 1:
        raise CreationPresentationError("unsupported creation presentation version")
    unknown = set(raw) - ({"version"} | _ALLOWED_SECTIONS)
    if unknown:
        raise CreationPresentationError(f"creation presentation has unknown root keys {sorted(unknown)}")
    for section in _ALLOWED_SECTIONS:
        value = raw.get(section) or {}
        if not isinstance(value, Mapping):
            raise CreationPresentationError(f"creation presentation {section} must be a mapping")
    stages = _mapping(raw.get("stages"))
    for stage_id, localized in stages.items():
        if not str(stage_id).strip() or not isinstance(localized, Mapping):
            raise CreationPresentationError("creation presentation contains an invalid stage")
        for locale, payload in localized.items():
            if not str(locale).strip() or not isinstance(payload, Mapping):
                raise CreationPresentationError(f"creation presentation stage {stage_id!r} locale must be a mapping")
            extra = set(payload) - _ALLOWED_STAGE_KEYS
            if extra:
                raise CreationPresentationError(
                    f"creation presentation stage {stage_id!r} has unknown keys {sorted(extra)}"
                )
            if not all(isinstance(value, str) for value in payload.values()):
                raise CreationPresentationError(f"creation presentation stage {stage_id!r} values must be text")
    return raw


def _locale_candidates(locale: str) -> tuple[str, ...]:
    locale_key = str(locale or "en").strip().casefold().replace("_", "-")
    language = locale_key.split("-", 1)[0]
    return tuple(dict.fromkeys((locale_key, language, "en")))


def stage_presentation(pack: Any, stage_id: str, locale: str) -> dict[str, str]:
    stages = _mapping(load_creation_presentation(pack).get("stages"))
    localized = _mapping(stages.get(stage_id))
    for candidate in _locale_candidates(locale):
        payload = localized.get(candidate)
        if isinstance(payload, Mapping):
            return {
                str(key): str(value).strip()
                for key, value in payload.items()
                if key in _ALLOWED_STAGE_KEYS and isinstance(value, str) and value.strip()
            }
    return {}


def presentation_label(pack: Any, section: str, key: str, locale: str, fallback: str) -> str:
    if section not in _ALLOWED_SECTIONS - {"stages"}:
        raise CreationPresentationError(f"unsupported presentation label section {section!r}")
    table = _mapping(load_creation_presentation(pack).get(section))
    localized = _mapping(table.get(key))
    for candidate in _locale_candidates(locale):
        value = localized.get(candidate)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return fallback
