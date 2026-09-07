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
from core.creation_flow import (
    CreationFlowStatus,
    creation_flow_duplicate_requirements,
    creation_flow_profile_reroll_attributes,
    creation_flow_status,
    load_creation_flow_spec,
)
from core.creation_layers import load_creation_layers, resolve_creation_layer_option
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
        # Prefer a name that matches the requested language when packs order their
        # aliases that way; otherwise the first declared human label is still better
        # than exposing an internal id.
        values = [str(value).strip() for value in names if isinstance(value, str) and value.strip()]
        if values:
            return values[0]
    return fallback


def _localized_rule_text(raw: Mapping[str, Any], locale: str) -> list[str]:
    """Best-effort authored explanatory text, without interpreting rule semantics.

    Packs often keep a small ``rules`` mapping beside an option.  Keys are pack-owned;
    clients must not know them.  We merely prefer values explicitly suffixed for the
    viewer language (``*_ru``/``*_en``), then fall back to ordinary human strings.
    This is presentation-only and never feeds any rule decision.
    """

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


def _stage(pack: RulePack, status: CreationFlowStatus):  # noqa: ANN202 - concrete dataclass is internal detail
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


def _choice_wire(group_id: str, raw: Mapping[str, Any], locale: str) -> dict[str, Any]:
    options = _mapping(raw.get("options"))
    option_rows: list[dict[str, Any]] = []
    for option_id, option_raw in options.items():
        if not isinstance(option_raw, Mapping):
            continue
        option_rows.append(
            {
                "id": str(option_id),
                "label": _localized_label(option_raw, locale, str(option_id)),
                "specialization": bool(
                    option_raw.get("skill_family") or option_raw.get("field_template")
                ),
            }
        )

    family = str(raw.get("skill_family") or raw.get("field_template") or "").strip()
    return {
        "id": group_id,
        "label": _localized_label(raw, locale, group_id),
        "free": not option_rows,
        "family": family,
        "options": option_rows,
    }


def _layer_option_wire(
    option_id: str,
    raw: Mapping[str, Any],
    locale: str,
    *,
    fixed: bool,
) -> dict[str, Any]:
    choices = _mapping(raw.get("choices"))
    payload: dict[str, Any] = {
        "id": option_id,
        "label": _localized_label(raw, locale, option_id),
        "fixed": fixed,
        "choices": [
            _choice_wire(str(group_id), group_raw, locale)
            for group_id, group_raw in choices.items()
            if isinstance(group_raw, Mapping)
        ],
    }
    detail = _localized_rule_text(raw, locale)
    if detail:
        payload["detail"] = detail
    source = raw.get("source")
    if isinstance(source, str) and source.strip():
        payload["source"] = source.strip()
    return payload


def _target_label(pack: RulePack, target: str, locale: str) -> str:
    family, separator, specialization = str(target).partition("::")
    if separator:
        return f"{pack.display_name(family, locale)} ({specialization})"
    return pack.display_name(target, locale)


def creation_catalog_surface(pack: RulePack, locale: str) -> dict[str, Any]:
    """Describe how this pack can begin deterministic profiled creation."""

    profiles = [
        {
            "id": str(profile_id),
            "label": _localized_label(raw, locale, str(profile_id)),
        }
        for profile_id, raw in _profile_map(pack).items()
        if isinstance(raw, Mapping)
    ]
    staged = load_creation_flow_spec(pack) is not None
    return {
        "staged": staged,
        "requires_profile": bool(profiles),
        "profiles": profiles,
    }


def creation_state_surface(pack: RulePack, character: Any, locale: str) -> dict[str, Any] | None:
    """Project the current persisted staged-creation step, or ``None`` if unmanaged."""

    status = creation_flow_status(pack, character)
    if status is None:
        return None
    spec = load_creation_flow_spec(pack)
    if spec is None:
        return None

    frame: dict[str, Any] = {
        "active": True,
        "complete": status.complete,
        "profile_id": status.profile_id,
        "stage_index": status.stage_index,
        "stage_count": len(spec.stages),
        "completed_stages": list(status.completed_stages),
        "stage": None,
    }
    if status.complete:
        return frame

    stage = _stage(pack, status)
    if stage is None:
        frame["complete"] = True
        return frame

    stage_wire: dict[str, Any] = {"id": stage.id, "kind": stage.kind}

    if stage.kind == "profile_reroll":
        attributes = getattr(character, "attributes", {}) or {}
        stage_wire["targets"] = [
            {
                "id": target,
                "label": pack.display_name(target, locale),
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
            _layer_option_wire(option_id, raw, locale, fixed=fixed)
            for option_id, raw in _layer_options(pack, status)
        ]

    elif stage.kind == "duplicates":
        stage_wire["requirements"] = [
            {
                "field": requirement.field,
                "count": requirement.count,
                "choices": [
                    {"id": choice, "label": pack.display_name(choice, locale)}
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
                    "target": quote.target,
                    "label": _target_label(pack, quote.target, locale),
                    "stage": quote.stage,
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
        if budget is not None:
            stage_wire["budget"] = {
                "total": budget.total,
                "used": budget.used,
                "remaining": budget.remaining,
            }

    frame["stage"] = stage_wire
    return frame
