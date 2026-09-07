"""Structured, private creation surfaces for rich clients.

The ordinary ``.create`` / ``.advance`` command lane remains the source of truth
for mutations.  These two hidden commands only expose the same pack-owned data
as semantic panel frames so a GUI never has to parse localized command prose or
hard-code a concrete ruleset.

``.__creation_catalog <system>`` advertises profiled creation choices before a
sheet exists. ``.__creation_state`` projects the current staged-creation step.
Both are private, read-only queries and deliberately stay out of ``.help``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.advancement_surface import available_advancement_surface
from core.character_manager import has_character
from core.creation_flow import (
    CreationFlowStatus,
    creation_flow_duplicate_requirements,
    creation_flow_profile_reroll_attributes,
    creation_flow_status,
    load_creation_flow_spec,
)
from core.creation_layers import load_creation_layers, resolve_creation_layer_option
from core.rulepacks import RulePack, load_rulepack
from core.starting_equipment import available_starting_items, starting_equipment_budget
from gateway.commands.types import CommandCtx, CommandSpec
from gateway.hub import Event

_CATALOG_WORD = "__creation_catalog"
_STATE_WORD = "__creation_state"


def _hidden_spec(canonical: str, handler) -> CommandSpec:  # noqa: ANN001
    return CommandSpec(
        canonical=canonical,
        handler=handler,
        aliases_en=[canonical],
        aliases_zh=[canonical],
        slash=None,
        help_key="commands.help.create",
        private_reply=True,
    )


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _profile_map(pack: RulePack) -> Mapping[str, Any]:
    return _mapping((pack.creation_constraints or {}).get("profiles"))


def _localized_label(raw: Mapping[str, Any], locale: str, fallback: str) -> str:
    display = _mapping(raw.get("display"))
    locale_key = locale.casefold()
    language = locale_key.split("-", 1)[0].split("_", 1)[0]
    for key in (locale_key, language, "en"):
        value = display.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    names = raw.get("names") or []
    if isinstance(names, (list, tuple)):
        for value in names:
            if isinstance(value, str) and value.strip():
                return value.strip()
    return fallback


def _stage(pack: RulePack, status: CreationFlowStatus):
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
    return {
        "id": option_id,
        "label": _localized_label(raw, locale, option_id),
        "fixed": fixed,
        "choices": [
            _choice_wire(str(group_id), group_raw, locale)
            for group_id, group_raw in choices.items()
            if isinstance(group_raw, Mapping)
        ],
    }


def _target_label(pack: RulePack, target: str, locale: str) -> str:
    family, separator, specialization = str(target).partition("::")
    if separator:
        return f"{pack.display_name(family, locale)} ({specialization})"
    return pack.display_name(target, locale)


def creation_catalog_frame(pack: RulePack, locale: str) -> dict[str, Any]:
    profiles = [
        {
            "id": str(profile_id),
            "label": _localized_label(raw, locale, str(profile_id)),
        }
        for profile_id, raw in _profile_map(pack).items()
        if isinstance(raw, Mapping)
    ]
    return {
        "type": "creation_catalog",
        "system": pack.system,
        "requires_profile": bool(profiles),
        "staged": load_creation_flow_spec(pack) is not None,
        "profiles": profiles,
    }


def creation_state_frame(pack: RulePack, character: Any, locale: str) -> dict[str, Any]:
    status = creation_flow_status(pack, character)
    if status is None:
        return {
            "type": "creation_state",
            "system": pack.system,
            "active": False,
            "complete": False,
            "profile_id": "",
            "completed_stages": [],
            "stage": None,
        }

    frame: dict[str, Any] = {
        "type": "creation_state",
        "system": pack.system,
        "active": True,
        "complete": status.complete,
        "profile_id": status.profile_id,
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


class CreationUiCommands:
    """Hidden read-only creation queries consumed by rich clients."""

    def resolve(self, text: str, locale: str):  # noqa: ANN201 - mirrors CommandRouter.resolve
        stripped = text.strip()
        prefix = next((item for item in self.prefixes if stripped.startswith(item)), "")
        if prefix:
            rest = stripped[len(prefix) :].lstrip()
            token, _separator, args = rest.partition(" ")
            token = token.casefold()
            if token == _CATALOG_WORD:
                return _hidden_spec(_CATALOG_WORD, self.cmd_creation_catalog), args.strip()
            if token == _STATE_WORD:
                return _hidden_spec(_STATE_WORD, self.cmd_creation_state), args.strip()
        return super().resolve(text, locale)

    async def cmd_creation_catalog(self, ctx: CommandCtx) -> str:
        system = ctx.args.strip()
        if not system:
            return ctx.fail("")
        try:
            pack = load_rulepack(system)
            frame = creation_catalog_frame(pack, ctx.locale)
        except Exception:
            return ctx.fail("")
        ctx.events.append(Event.panel(frame, private=True))
        return ctx.fail("")

    async def cmd_creation_state(self, ctx: CommandCtx) -> str:
        character = await ctx.services.characters.get_character(ctx.user_id, ctx.chat_key)
        if not has_character(character):
            ctx.events.append(
                Event.panel(
                    {
                        "type": "creation_state",
                        "system": "",
                        "active": False,
                        "complete": False,
                        "profile_id": "",
                        "completed_stages": [],
                        "stage": None,
                    },
                    private=True,
                )
            )
            return ctx.fail("")
        try:
            pack = load_rulepack(character.system)
            frame = creation_state_frame(pack, character, ctx.locale)
        except Exception:
            return ctx.fail("")
        ctx.events.append(Event.panel(frame, private=True))
        return ctx.fail("")
