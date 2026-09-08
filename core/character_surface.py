"""Read-only, rulepack-driven rich character sheet projection.

The core sheet keeps canonical storage keys and values. Rich clients need a little more:
localized skill labels plus the list-like fields players actually consult at the table.
This module projects those values without teaching the network layer or clients any rule
system vocabulary.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.creation_presentation import load_creation_presentation, presentation_label


def _term_label(pack: Any, target: str, locale: str, presentation: Mapping[str, Any]) -> str:
    family, separator, specialization = str(target).partition("::")
    if separator:
        base = presentation_label(
            pack,
            "terms",
            family,
            locale,
            pack.display_name(family, locale),
            presentation=presentation,
        )
        return f"{base} ({specialization})"
    return presentation_label(
        pack,
        "terms",
        str(target),
        locale,
        pack.display_name(str(target), locale),
        presentation=presentation,
    )


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item).strip() for item in value if isinstance(item, str) and item.strip()]


def _field_list(pack: Any, character: Any, canonical: str) -> list[str]:
    spec = getattr(pack, "sheet_spec", None)
    if spec is None:
        return []
    field_name = spec.field_keys.get(canonical)
    if not field_name:
        return []
    return _string_list(getattr(character, field_name, None))


def character_detail_surface(pack: Any, character: Any, locale: str = "en") -> dict[str, Any]:
    """Return additive rich-sheet fields for one viewer locale.

    The shape is intentionally generic and optional. Existing protocol consumers can
    ignore it; Studio uses it to populate Skills, Talents and Equipment tabs.
    """

    presentation = load_creation_presentation(pack)
    skills_raw = getattr(character, "skills", None)
    skills = dict(skills_raw) if isinstance(skills_raw, dict) else {}
    payload: dict[str, Any] = {
        "skills": skills,
        "skill_labels": {
            str(key): _term_label(pack, str(key), locale, presentation)
            for key in skills
        },
        "talents": _field_list(pack, character, "Talents"),
        "equipment": _string_list(getattr(character, "equipment", None)),
    }
    return payload
