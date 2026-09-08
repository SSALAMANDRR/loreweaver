"""Read-only, rulepack-driven character-creation projection for rich clients.

Creation mutation remains owned by the existing deterministic primitives and command
lane.  This module only turns their persisted state and pack declarations into a
small JSON-safe read model suitable for ``state`` frames.  A client therefore needs
no rule-system vocabulary and never parses localized command prose.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.advancement_surface import available_advancement_surface
from core.character_context import character_context_surface
from core.creation_flow import (
    CreationFlowStatus,
    creation_flow_duplicate_requirements,
    creation_flow_profile_reroll_attributes,
    creation_flow_status,
    load_creation_flow_spec,
)
from core.creation_layers import load_creation_layers, resolve_creation_layer_option
from core.creation_presentation import (
    load_creation_presentation,
    presentation_label,
    stage_presentation,
)
from core.rulepacks import RulePack
from core.starting_equipment import available_starting_items, starting_equipment_budget


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _profile_map(pack: RulePack) -> Mapping[str, Any]:
    return _mapping((pack.creation_constraints or {}).get("profiles"))


def _localized_label(raw: Mapping[str, Any], locale: str, fallback: str) -> str:
    display = _mapping(raw.get("display"))
    locale_key = str(locale or "en").casefold()
    language = locale_key.split("-", 1)[0].split("_", 1)[0]
    for key in (locale_key, language, "en"):
        value = display.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    names = raw.get("names") or []
    if isinstance(names, (list, tuple)):
        values = [str(value).strip() for value in names if isinstance(value, str) and value.strip()]
        if values:
            return values[0]
    return fallback


def _localized_rule_text(raw: Mapping[str, Any], locale: str) -> list[str]:
    """Best-effort authored explanatory text, without interpreting rule semantics."""

    rules = _mapping(raw.get("rules"))
    if not rules:
        return []
    language = str(locale or "en").casefold().split("-", 1)[0].split("_", 1)[0]
    localized: list[str] = []
    fallback: list[str] = []
    for key, value in rules.items():
        if not isinstance(value, str) or not value.strip():
            continue
        text = value.strip()
        key_text = str(key).casefold()
        if key_text.endswith(f"_{language}"):
            localized.append(text)
        elif not any(key_text.endswith(f"_{code}") for code in ("en", "ru", "zh")):
            fallback.append(text)
    return localized or fallback


def _localized_lines(raw: Mapping[str, Any], locale: str) -> list[str]:
    """Resolve a localized authored string/list used for profile previews."""

    locale_key = str(locale or "en").strip().casefold().replace("_", "-")
    language = locale_key.split("-", 1)[0]
    for key in tuple(dict.fromkeys((locale_key, language, "en"))):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        if isinstance(value, (list, tuple)):
            rows = [str(item).strip() for item in value if isinstance(item, str) and item.strip()]
            if rows:
                return rows
    return []


def _stage(pack: RulePack, status: CreationFlowStatus):  # noqa: ANN202 - internal dataclass
    spec = load_creation_flow_spec(pack)
    if spec is None or status.complete or status.stage_index >= len(spec.stages):
        return None
    return spec.stages[status.stage_index]


def _layer_options(pack: RulePack, status: CreationFlowStatus) -> tuple[tuple[str, Mapping[str, Any]], ...]:
    stage = _stage(pack, status)
    if stage is None or stage.kind != "layer":
        return ()
    if stage.option_from_profile:
        resolved = resolve_creation_layer_option(pack, stage.layer_id, status.profile_id)
        return (resolved,) if resolved is not None else ()

    layer = _mapping(load_creation_layers(pack).get(stage.layer_id))
    options = _mapping(layer.get("options"))
    return tuple(
        (str(option_id), raw)
        for option_id, raw in options.items()
        if isinstance(raw, Mapping)
    )


def _profile_bound_option(pack: RulePack, spec: Any, profile_id: str) -> Mapping[str, Any] | None:
    """Find a later layer explicitly bound to the selected profile, generically."""

    if spec is None:
        return None
    for stage in spec.stages:
        if stage.kind != "layer" or not stage.option_from_profile:
            continue
        resolved = resolve_creation_layer_option(pack, stage.layer_id, profile_id)
        if resolved is not None and resolved[0] == profile_id:
            return resolved[1]
    return None


def _term_label(
    pack: RulePack,
    target: str,
    locale: str,
    presentation: Mapping[str, Any],
) -> str:
    return presentation_label(
        pack,
        "terms",
        target,
        locale,
        pack.display_name(target, locale),
        presentation=presentation,
    )


def _target_label(
    pack: RulePack,
    target: str,
    locale: str,
    presentation: Mapping[str, Any],
) -> str:
    family, separator, specialization = str(target).partition("::")
    if separator:
        return f"{_term_label(pack, family, locale, presentation)} ({specialization})"
    return _term_label(pack, target, locale, presentation)


def _effect_wire(
    pack: RulePack,
    raw: Mapping[str, Any],
    locale: str,
    presentation: Mapping[str, Any],
) -> dict[str, Any]:
    """Project declared creation effects without inventing system semantics.

    The mutation layer already has a fixed generic vocabulary (attributes, skills,
    append_fields and equipment).  Rich clients need the same open information before
    the player clicks a choice, so this turns those declarations into a presentation-
    friendly shape while leaving application of the effects entirely to creation_layers.
    """

    effects = _mapping(raw.get("effects"))
    if not effects:
        return {}

    payload: dict[str, Any] = {}

    grants: list[str] = []
    for values in _mapping(effects.get("append_fields")).values():
        if isinstance(values, (list, tuple)):
            grants.extend(str(value).strip() for value in values if str(value).strip())
    if grants:
        payload["grants"] = grants

    equipment = effects.get("equipment") or []
    if isinstance(equipment, (list, tuple)):
        rows = [str(value).strip() for value in equipment if str(value).strip()]
        if rows:
            payload["equipment"] = rows

    skills: list[dict[str, Any]] = []
    for target, value in _mapping(effects.get("skills")).items():
        skills.append(
            {
                "label": _target_label(pack, str(target), locale, presentation),
                "value": value,
            }
        )
    if skills:
        payload["skills"] = skills

    attributes: list[dict[str, Any]] = []
    for target, value in _mapping(effects.get("attributes")).items():
        attributes.append(
            {
                "label": _term_label(pack, str(target), locale, presentation),
                "value": value,
            }
        )
    if attributes:
        payload["attributes"] = attributes

    return payload


def _choice_wire(
    pack: RulePack,
    group_id: str,
    raw: Mapping[str, Any],
    locale: str,
    presentation: Mapping[str, Any],
) -> dict[str, Any]:
    options = _mapping(raw.get("options"))
    option_rows: list[dict[str, Any]] = []
    for option_id, option_raw in options.items():
        if not isinstance(option_raw, Mapping):
            continue
        option_id_text = str(option_id)
        authored = _localized_label(option_raw, locale, option_id_text)
        row: dict[str, Any] = {
            "id": option_id_text,
            "label": presentation_label(
                pack,
                "choice_options",
                option_id_text,
                locale,
                authored,
                presentation=presentation,
            ),
            "specialization": bool(
                option_raw.get("skill_family") or option_raw.get("field_template")
            ),
        }
        effect = _effect_wire(pack, option_raw, locale, presentation)
        if effect:
            row["effect"] = effect
        option_rows.append(row)

    family = str(raw.get("skill_family") or raw.get("field_template") or "").strip()
    authored = _localized_label(raw, locale, group_id)
    return {
        "id": group_id,
        "label": presentation_label(
            pack,
            "choice_groups",
            group_id,
            locale,
            authored,
            presentation=presentation,
        ),
        "free": not option_rows,
        "family": family,
        "options": option_rows,
    }


def _layer_option_wire(
    pack: RulePack,
    option_id: str,
    raw: Mapping[str, Any],
    locale: str,
    presentation: Mapping[str, Any],
    *,
    fixed: bool,
) -> dict[str, Any]:
    choices = _mapping(raw.get("choices"))
    payload: dict[str, Any] = {
        "id": option_id,
        "label": _localized_label(raw, locale, option_id),
        "fixed": fixed,
        "choices": [
            _choice_wire(pack, str(group_id), group_raw, locale, presentation)
            for group_id, group_raw in choices.items()
            if isinstance(group_raw, Mapping)
        ],
    }
    detail = _localized_rule_text(raw, locale)
    if detail:
        payload["detail"] = detail
    effect = _effect_wire(pack, raw, locale, presentation)
    if effect:
        payload["effect"] = effect
    source = raw.get("source")
    if isinstance(source, str) and source.strip():
        payload["source"] = source.strip()
    return payload


def _current_list_field(
    pack: RulePack,
    character: Any,
    canonical_field: str,
    locale: str,
    presentation: Mapping[str, Any],
) -> list[str]:
    spec = getattr(pack, "sheet_spec", None)
    field_name = spec.field_keys.get(canonical_field) if spec is not None else None
    if not field_name:
        return []
    raw = getattr(character, field_name, None)
    if not isinstance(raw, (list, tuple)):
        return []
    return [
        _term_label(pack, str(value), locale, presentation)
        for value in raw
        if isinstance(value, str) and value.strip()
    ]


def creation_catalog_surface(pack: RulePack, locale: str) -> dict[str, Any]:
    """Describe how this pack can begin deterministic profiled creation."""

    spec = load_creation_flow_spec(pack)
    presentation_data = load_creation_presentation(pack)
    profile_notes = _mapping(presentation_data.get("profiles"))
    profiles: list[dict[str, Any]] = []
    for profile_id, raw in _profile_map(pack).items():
        if not isinstance(raw, Mapping):
            continue
        profile_id_text = str(profile_id)
        bound = _profile_bound_option(pack, spec, profile_id_text)
        label_source = bound if bound is not None else raw
        row: dict[str, Any] = {
            "id": profile_id_text,
            "label": _localized_label(label_source, locale, profile_id_text),
        }
        detail = _localized_lines(_mapping(profile_notes.get(profile_id_text)), locale)
        if detail:
            row["detail"] = detail
        if bound is not None:
            source = bound.get("source")
            if isinstance(source, str) and source.strip():
                row["source"] = source.strip()
            effect = _effect_wire(pack, bound, locale, presentation_data)
            if effect:
                row["effect"] = effect
            choices = _mapping(bound.get("choices"))
            choice_rows = [
                _choice_wire(pack, str(group_id), group_raw, locale, presentation_data)
                for group_id, group_raw in choices.items()
                if isinstance(group_raw, Mapping)
            ]
            if choice_rows:
                row["choices"] = choice_rows
        profiles.append(row)

    payload: dict[str, Any] = {
        "staged": spec is not None,
        "requires_profile": bool(profiles),
        "profiles": profiles,
    }
    if spec is not None and spec.stages:
        presentation = stage_presentation(
            pack,
            spec.stages[0].id,
            locale,
            presentation=presentation_data,
        )
        if presentation:
            payload["presentation"] = presentation
    return payload


def creation_state_surface(pack: RulePack, character: Any, locale: str) -> dict[str, Any] | None:
    """Project the current persisted staged-creation step, or ``None`` if unmanaged.

    Optional narrative character context is attached to the same reconnect-safe
    lifecycle surface. It is pack-declared and deliberately separate from mechanics:
    a client can ask who this character is *now* without core learning setting lore.
    """

    status = creation_flow_status(pack, character)
    if status is None:
        return None
    spec = load_creation_flow_spec(pack)
    if spec is None:
        return None
    presentation_data = load_creation_presentation(pack)

    frame: dict[str, Any] = {
        "active": True,
        "complete": status.complete,
        "profile_id": status.profile_id,
        "stage_index": status.stage_index,
        "stage_count": len(spec.stages),
        "completed_stages": list(status.completed_stages),
        "stage": None,
    }
    context = character_context_surface(pack, character, locale)
    if context is not None:
        frame["context"] = context
    if status.complete:
        return frame

    stage = _stage(pack, status)
    if stage is None:
        frame["complete"] = True
        return frame

    stage_wire: dict[str, Any] = {"id": stage.id, "kind": stage.kind}
    presentation = stage_presentation(
        pack,
        stage.id,
        locale,
        presentation=presentation_data,
    )
    if presentation:
        stage_wire["presentation"] = presentation

    if stage.kind == "profile_reroll":
        attributes = getattr(character, "attributes", {}) or {}
        stage_wire["targets"] = [
            {
                "id": target,
                "label": _term_label(pack, target, locale, presentation_data),
                "value": attributes.get(target),
            }
            for target in creation_flow_profile_reroll_attributes(pack, character)
        ]
        stage_wire["can_skip"] = True

    elif stage.kind == "layer":
        fixed = bool(stage.option_from_profile)
        stage_wire["layer"] = stage.layer_id
        stage_wire["fixed"] = fixed
        stage_wire["options"] = [
            _layer_option_wire(
                pack,
                option_id,
                raw,
                locale,
                presentation_data,
                fixed=fixed,
            )
            for option_id, raw in _layer_options(pack, status)
        ]

    elif stage.kind == "duplicates":
        stage_wire["requirements"] = [
            {
                "field": requirement.field,
                "count": requirement.count,
                "current": _current_list_field(
                    pack,
                    character,
                    requirement.field,
                    locale,
                    presentation_data,
                ),
                "choices": [
                    {
                        "id": choice,
                        "label": _term_label(pack, choice, locale, presentation_data),
                    }
                    for choice in requirement.choices
                ],
            }
            for requirement in creation_flow_duplicate_requirements(pack, character)
        ]

    elif stage.kind == "advancement":
        surface = available_advancement_surface(pack, character)
        if surface is not None:
            stage_wire["budget"] = {
                "starting": surface.budget.starting_xp,
                "available": surface.budget.available_xp,
                "spent": surface.budget.spent_xp,
            }
            stage_wire["purchases"] = [
                {
                    "category": quote.category,
                    "category_label": presentation_label(
                        pack,
                        "advancement_categories",
                        quote.category,
                        locale,
                        quote.category,
                        presentation=presentation_data,
                    ),
                    "target": quote.target,
                    "label": _target_label(pack, quote.target, locale, presentation_data),
                    "stage": quote.stage,
                    "stage_label": presentation_label(
                        pack,
                        "advancement_stages",
                        quote.stage,
                        locale,
                        quote.stage,
                        presentation=presentation_data,
                    ),
                    "current": quote.current_value,
                    "next": quote.next_value,
                    "cost": quote.cost,
                    "affordable": quote.cost <= surface.budget.available_xp,
                }
                for quote in surface.purchases
            ]

    elif stage.kind == "starting_equipment":
        budget = starting_equipment_budget(pack, character)
        stage_wire["items"] = [
            {
                "id": item.id,
                "label": item.name,
                "kind": item.kind,
                "availability": item.availability,
            }
            for item in available_starting_items(pack, character)
        ]
        equipment = getattr(character, "equipment", None)
        if isinstance(equipment, list):
            stage_wire["inventory"] = [str(item) for item in equipment]
        if budget is not None:
            stage_wire["budget"] = {
                "total": budget.total,
                "used": budget.used,
                "remaining": budget.remaining,
            }

    frame["stage"] = stage_wire
    return frame
