"""Optional rulepack-authored help for rich character sheets.

Mechanics remain in the ordinary rulepack and sidecars. This file only loads short,
localized explanations that clients may expose as tooltips or contextual help.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from core.yaml_safety import safe_load_no_aliases

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BUILTIN_DATA_ROOT = _REPO_ROOT / "rulepacks" / "data"
_ALLOWED_SECTIONS = frozenset({"attributes", "skills", "equipment"})


class SheetPresentationError(ValueError):
    """A sheet-presentation sidecar is malformed."""


def _candidate_sidecars(pack: Any, data_root: Path | None = None) -> list[Path]:
    system = str(getattr(pack, "system", "")).strip()
    if not system:
        raise SheetPresentationError("pack has no system id")  # i18n-exempt: internal validation
    if any(part in system for part in ("/", "\\", "..")):
        raise SheetPresentationError("pack system id is not safe for a presentation path")  # i18n-exempt
    if data_root is not None:
        return [Path(data_root) / system / "sheet_presentation.yaml"]

    candidates: list[Path] = []
    try:
        import core.rulepacks as rulepacks

        user_root = getattr(rulepacks, "_USER_RULEPACK_DIR", None)
    except Exception:  # pragma: no cover - defensive import boundary
        user_root = None
    if user_root is not None:
        candidates.append(Path(user_root) / "data" / system / "sheet_presentation.yaml")
    candidates.append(_BUILTIN_DATA_ROOT / system / "sheet_presentation.yaml")
    return candidates


def load_sheet_presentation(pack: Any, *, data_root: Path | None = None) -> Mapping[str, Any]:
    path = next((candidate for candidate in _candidate_sidecars(pack, data_root) if candidate.is_file()), None)
    if path is None:
        return {}
    try:
        raw = safe_load_no_aliases(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise SheetPresentationError(f"could not load sheet presentation {path.name!r}: {exc}") from exc  # i18n-exempt
    if not isinstance(raw, Mapping):
        raise SheetPresentationError("sheet presentation root must be a mapping")  # i18n-exempt
    if int(raw.get("version", 1)) != 1:
        raise SheetPresentationError("unsupported sheet presentation version")  # i18n-exempt
    unknown = set(raw) - ({"version"} | _ALLOWED_SECTIONS)
    if unknown:
        raise SheetPresentationError(f"sheet presentation has unknown root keys {sorted(unknown)}")  # i18n-exempt
    for section in _ALLOWED_SECTIONS:
        value = raw.get(section) or {}
        if not isinstance(value, Mapping):
            raise SheetPresentationError(f"sheet presentation {section} must be a mapping")  # i18n-exempt
        for key, localized in value.items():
            if not str(key).strip() or not isinstance(localized, Mapping):
                raise SheetPresentationError(f"sheet presentation {section} contains an invalid entry")  # i18n-exempt
            if not all(isinstance(locale, str) and isinstance(text, str) for locale, text in localized.items()):
                raise SheetPresentationError(f"sheet presentation {section}.{key} must map locales to text")  # i18n-exempt
    return raw


def _locale_candidates(locale: str) -> tuple[str, ...]:
    locale_key = str(locale or "en").strip().casefold().replace("_", "-")
    language = locale_key.split("-", 1)[0]
    return tuple(dict.fromkeys((locale_key, language, "en")))


def sheet_help(
    pack: Any,
    section: str,
    key: str,
    locale: str,
    *,
    presentation: Mapping[str, Any] | None = None,
) -> str:
    """Resolve one authored help string, falling back to an empty string."""

    if section not in _ALLOWED_SECTIONS:
        raise SheetPresentationError(f"unsupported sheet help section {section!r}")  # i18n-exempt
    data = presentation if presentation is not None else load_sheet_presentation(pack)
    table = data.get(section) or {}
    localized = table.get(key) if isinstance(table, Mapping) else None
    if not isinstance(localized, Mapping):
        return ""
    for candidate in _locale_candidates(locale):
        value = localized.get(candidate)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""
