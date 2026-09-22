"""Read-only, rulepack-driven rich character sheet projection.

The core sheet keeps canonical storage keys and values. Rich clients need a little more:
localized labels, concise authored help, and the list-like fields players actually consult
at the table. This module projects those values without teaching the network layer or
clients any rule-system vocabulary.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.creation_presentation import load_creation_presentation, presentation_label
from core.item_model import ItemInstance, equipment_entry_label, load_item_catalog
from core.sheet_presentation import load_sheet_presentation, sheet_help
from core.starting_equipment import load_starting_equipment_spec


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


def _normalize(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().replace("_", " ").split())


def _equipment_details(
    pack: Any,
    equipment: list[Any],
    locale: str,
    sheet_presentation: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Describe typed item instances and legacy inventory strings generically."""

    try:
        catalog = load_starting_equipment_spec(pack)
    except Exception:
        catalog = None
    try:
        profiles = load_item_catalog(pack)
    except Exception:
        profiles = None

    if catalog is None and profiles is None:
        return {}

    surfaces: dict[str, Any] = {}
    if catalog is not None:
        for item in catalog.items.values():
            for surface in (item.id, item.name, *item.aliases):
                surfaces[_normalize(surface)] = item

    result: dict[str, dict[str, Any]] = {}
    for stored in equipment:
        profile = None
        if isinstance(stored, ItemInstance) and profiles is not None:
            profile = profiles.get(stored.profile_id)
            item = surfaces.get(_normalize(profile.id)) if profile is not None else None
        else:
            item = surfaces.get(_normalize(stored))
        if item is None and profile is None:
            continue
        detail: dict[str, Any] = {
            "kind": profile.kind if profile is not None else item.kind,
            "availability": profile.availability if profile is not None else item.availability,
        }
        source = profile.source_reference if profile is not None else item.source
        if source:
            detail["source"] = source
        if profile is not None:
            detail["profile_id"] = profile.id
            detail["instance_id"] = stored.instance_id
            if stored.state:
                detail["state"] = dict(stored.state)
            if profile.damage_expression is not None:
                detail["damage_expression"] = profile.damage_expression
            if profile.damage_type is not None:
                detail["damage_type"] = profile.damage_type
            if profile.armor_by_location:
                detail["armor_by_location"] = dict(profile.armor_by_location)
        help_text = sheet_help(
            pack,
            "equipment",
            profile.id if profile is not None else item.id,
            locale,
            presentation=sheet_presentation,
        )
        if help_text:
            detail["help"] = help_text
        result[equipment_entry_label(stored, profiles)] = detail
    return result


def character_detail_surface(pack: Any, character: Any, locale: str = "en") -> dict[str, Any]:
    """Return additive rich-sheet fields for one viewer locale.

    The shape is intentionally generic and optional. Existing protocol consumers can
    ignore it; Studio uses it to populate sheet tabs and hover help.
    """

    creation_presentation = load_creation_presentation(pack)
    sheet_presentation = load_sheet_presentation(pack)
    skills_raw = getattr(character, "skills", None)
    skills = dict(skills_raw) if isinstance(skills_raw, dict) else {}
    stored_equipment = getattr(character, "equipment", None)
    equipment = []
    if isinstance(stored_equipment, list):
        try:
            profiles = load_item_catalog(pack)
        except Exception:
            profiles = None
        equipment = [equipment_entry_label(item, profiles) for item in stored_equipment]
    attributes_raw = getattr(character, "attributes", None)
    attribute_keys = attributes_raw.keys() if isinstance(attributes_raw, dict) else ()

    skill_help_map: dict[str, str] = {}
    for key in skills:
        canonical = str(key)
        family = canonical.partition("::")[0]
        help_text = sheet_help(
            pack,
            "skills",
            canonical,
            locale,
            presentation=sheet_presentation,
        ) or sheet_help(
            pack,
            "skills",
            family,
            locale,
            presentation=sheet_presentation,
        )
        if help_text:
            skill_help_map[canonical] = help_text

    attribute_help = {
        str(key): help_text
        for key in attribute_keys
        if (help_text := sheet_help(
            pack,
            "attributes",
            str(key),
            locale,
            presentation=sheet_presentation,
        ))
    }

    payload: dict[str, Any] = {
        "attribute_help": attribute_help,
        "skills": skills,
        "skill_labels": {
            str(key): _term_label(pack, str(key), locale, creation_presentation)
            for key in skills
        },
        "skill_help": skill_help_map,
        "talents": _field_list(pack, character, "Talents"),
        "equipment": equipment,
        "equipment_details": _equipment_details(
            pack,
            stored_equipment if isinstance(stored_equipment, list) else [],
            locale,
            sheet_presentation,
        ),
    }
    return payload
