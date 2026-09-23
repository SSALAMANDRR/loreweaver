"""Generic action transport for pack-declared deterministic combat."""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from typing import Any

from agent.context import AgentCtx
from agent.services import Services
from core.character_manager import CharacterSheet, character_resources
from core.combat import (
    ActionRequest,
    CombatantState,
    CombatState,
    CombatValidationError,
    apply_state_delta,
    create_combat_state,
    resolve_aim,
    resolve_first_shot,
    resolve_melee_attack,
    resolve_reload,
)
from core.item_model import ItemInstance, load_item_catalog
from core.rulepacks import load_rulepack
from infra.i18n import get_i18n
from infra.room_facets import STORAGE_ROOM_STATE, RoomStateFacet

COMBAT_STATE_KEY = "combat_state"

ROOM_FACETS = (
    RoomStateFacet(
        name="combat_state",
        owner=__name__,
        reset_scope="story",
        state_keys=frozenset({COMBAT_STATE_KEY}),
        storages=frozenset({STORAGE_ROOM_STATE}),
    ),
)


def _state_from_json(raw: str) -> CombatState:
    data = json.loads(raw)
    return CombatState(
        round_number=data["round_number"],
        current_actor=data["current_actor"],
        combatants={name: CombatantState(**row) for name, row in data["combatants"].items()},
    )


def _label(value: Any, locale: str, fallback: str) -> str:
    if isinstance(value, dict):
        return str(value.get(locale) or value.get("en") or fallback)
    return fallback


def available_actions(
    pack: Any, actor: CharacterSheet, targets: list[str], locale: str, state: CombatState | None = None
) -> list[dict[str, Any]]:
    """Expose only modes supported by the pack, item profile and actor inventory."""
    from core.combat import _combat_data

    try:
        data = _combat_data(pack)
        catalog = load_item_catalog(pack)
    except (CombatValidationError, ValueError):
        return []
    presentation = data.get("presentation") or {}
    actor_budget = state.combatants.get(actor.name) if state is not None else None
    if state is not None and (state.current_actor != actor.name or actor_budget is None):
        return []
    entries: list[dict[str, Any]] = []
    for action, definition in (data.get("actions") or {}).items():
        modes: list[dict[str, Any]] = []
        for mode, contract in (definition.get("modes") or {}).items():
            if actor_budget is not None and actor_budget.action_budget < contract.get("action_cost", 0):
                continue
            weapons: list[dict[str, str]] = []
            for item in actor.equipment:
                if not isinstance(item, ItemInstance):
                    continue
                profile = catalog.get(item.profile_id)
                if profile is None or profile.kind != "weapon" or not profile.is_usable_for_resolution:
                    continue
                if action in {"ranged_attack", "melee_attack"} and profile.id not in contract.get("weapon_profiles", []):
                    continue
                if action == "ranged_attack" and profile.rate_of_fire.get(mode) is None:
                    continue
                if action == "ranged_attack" and (
                    item.current_ammo is None or item.current_ammo < profile.rate_of_fire[mode]
                ):
                    continue
                if action == "reload" and (profile.reload != mode or profile.clip_size is None):
                    continue
                if action == "reload" and (item.current_ammo is None or item.current_ammo >= profile.clip_size):
                    continue
                weapons.append({"id": item.instance_id, "label": profile.name})
            if weapons:
                mode_labels = (presentation.get("modes") or {}).get(mode)
                reactions = [
                    {"id": kind, "label": _label((presentation.get("reactions") or {}).get(kind), locale, kind)}
                    for kind in (contract.get("reactions") or {}) if kind != "basic"
                ]
                modes.append({
                    "id": mode,
                    "label": _label(mode_labels, locale, mode),
                    "weapons": weapons,
                    "reactions": reactions,
                })
        if modes:
            entries.append({
                "id": action,
                "label": _label((presentation.get("actions") or {}).get(action), locale, action),
                "modes": modes,
                "targets": targets if action in {"ranged_attack", "melee_attack"} else [],
            })
    return entries


async def combat_surface(services: Services, ctx: AgentCtx, actor: CharacterSheet) -> dict[str, Any] | None:
    try:
        pack = load_rulepack(actor.system)
        roster = await services.characters.get_party_roster(ctx.chat_key)
        targets = [
            str(member["name"]) for member in roster
            if member.get("name") != actor.name and member.get("system") == actor.system
        ]
        raw = await services.store.state_get(ctx.chat_key, COMBAT_STATE_KEY)
        state = _state_from_json(raw) if raw else None
        actions = available_actions(pack, actor, targets, ctx.locale, state)
        if not actions and state is None:
            return None
        return {
            "actor": actor.name,
            "actions": actions,
            "state": asdict(state) if state is not None else None,
        }
    except (ValueError, KeyError, TypeError):
        return None


def _failure(frame: dict[str, Any], reason: str, locale: str) -> dict[str, Any]:
    key = "combat.invalid"
    for fragment, suffix in (
        ("active character", "actor"), ("owned by", "actor"),
        ("target", "target"), ("weapon", "weapon"), ("ammunition", "ammo"),
        ("action budget", "budget"), ("reaction", "reaction"), ("stale", "stale"),
    ):
        if fragment in reason:
            key = f"combat.invalid.{suffix}"
            break
    return {
        "type": "action_result",
        "id": str(frame.get("id") or ""),
        "ok": False,
        "result": None,
        "validation_failure": get_i18n(locale).t(key),
    }


async def resolve_action(services: Services, ctx: AgentCtx, frame: dict[str, Any]) -> dict[str, Any]:
    """Resolve and commit a client action; return the engine-authored wire result."""
    try:
        action = frame["action"]
        mode = frame["mode"]
        target_name = frame.get("target") or ""
        weapon_id = frame["weapon_instance_id"]
        if not all(isinstance(value, str) and 0 < len(value) <= 120 for value in (action, mode, weapon_id)):
            raise CombatValidationError("action, mode and weapon are required")  # i18n-exempt: internal validation diagnostic
        if not isinstance(target_name, str) or len(target_name) > 120 or len(weapon_id) > 120:
            raise CombatValidationError("invalid target or weapon")
        if action not in {"ranged_attack", "melee_attack", "aim", "reload"}:
            raise CombatValidationError("unknown action")
        if action in {"ranged_attack", "melee_attack"} and not target_name:
            raise CombatValidationError("target is required")
        reaction = frame.get("reaction_type")
        if reaction is not None and (not isinstance(reaction, str) or len(reaction) > 120):
            raise CombatValidationError("invalid reaction")
        distance = frame.get("distance")
        if distance is not None and (type(distance) is not int or distance < 0):
            raise CombatValidationError("invalid distance")
        active_name = await services.store.state_get(ctx.chat_key, f"active_character.{ctx.uid()}")
        if not active_name or active_name != frame.get("actor"):
            raise CombatValidationError("actor is not the active character")  # i18n-exempt: internal validation diagnostic
        actor_row = await services.store.doc_get(ctx.chat_key, "sheet", active_name)
        if actor_row is None or json.loads(actor_row["data"]).get("owner") != ctx.uid():
            raise CombatValidationError("actor is not owned by the caller")  # i18n-exempt: internal validation diagnostic
        actor = CharacterSheet.from_dict(json.loads(actor_row["data"]))
        roster_members = await services.characters.get_party_roster(ctx.chat_key)
        visible_targets = {
            str(member["name"]) for member in roster_members
            if member.get("system") == actor.system and member.get("name") != actor.name
        }
        if target_name and target_name not in visible_targets:
            raise CombatValidationError("target is unavailable")
        target_row = await services.store.doc_get(ctx.chat_key, "sheet", target_name) if target_name else None
        if target_name and target_row is None:
            raise CombatValidationError("target is unavailable")
        target = CharacterSheet.from_dict(json.loads(target_row["data"])) if target_row else None
        pack = load_rulepack(actor.system)
        raw_state = await services.store.state_get(ctx.chat_key, COMBAT_STATE_KEY)
        if raw_state:
            state = _state_from_json(raw_state)
        else:
            names = [actor.name, *sorted(visible_targets)]
            state = create_combat_state(names, current_actor=actor.name, pack=pack)
        request = ActionRequest(
            actor=actor,
            target=target,
            weapon_instance_id=weapon_id,
            mode=mode,
            reaction_type=reaction,
            distance=distance,
        )
        resolver = {
            "ranged_attack": resolve_first_shot,
            "melee_attack": resolve_melee_attack,
            "aim": resolve_aim,
            "reload": resolve_reload,
        }[action]
        result = resolver(request, combat_state=state, pack=pack)
        if not result.ok:
            raise CombatValidationError(result.validation_failure or "invalid action")
        apply_state_delta(request, result, combat_state=state, pack=pack)
        updates = []
        for row, sheet in ((actor_row, actor), (target_row, target)):
            if row is None or sheet is None:
                continue
            data = dict(sheet.to_dict(), owner=json.loads(row["data"]).get("owner", ""))
            meta = json.loads(row["meta"])
            meta["modified"] = time.time()
            updates.append((sheet.name, row["data"], json.dumps(data, ensure_ascii=False), json.dumps(meta, ensure_ascii=False)))
        old_roster = await services.store.state_get(ctx.chat_key, "party_roster")
        roster = json.loads(old_roster) if old_roster else {}
        for sheet in (actor, target):
            if sheet is None or sheet.name not in roster:
                continue
            roster[sheet.name]["resources"] = character_resources(sheet)
        state_rows = [
            (COMBAT_STATE_KEY, raw_state, json.dumps(asdict(state), ensure_ascii=False)),
            ("party_roster", old_roster, json.dumps(roster, ensure_ascii=False)),
        ]
        from core.combat import _combat_data

        presentation = (_combat_data(pack).get("presentation") or {})
        weapon_profile = load_item_catalog(pack).get(result.weapon_profile_id)
        labels = {
            "action": _label((presentation.get("actions") or {}).get(action), ctx.locale, action),
            "mode": _label((presentation.get("modes") or {}).get(mode), ctx.locale, mode),
            "weapon": weapon_profile.name if weapon_profile is not None else result.weapon_profile_id,
            "locations": {
                hit.location: _label((presentation.get("locations") or {}).get(hit.location), ctx.locale, hit.location)
                for hit in result.hits
            },
            "reaction": _label(
                (presentation.get("reactions") or {}).get(reaction), ctx.locale, reaction or ""
            ) if reaction else "",
        }
        if not await services.store.commit_combat_rows(ctx.chat_key, documents=updates, state=state_rows):
            raise CombatValidationError("stale combat state")
        return {
            "type": "action_result",
            "id": str(frame.get("id") or ""),
            "ok": True,
            "result": asdict(result),
            "labels": labels,
            "validation_failure": None,
        }
    except (CombatValidationError, ValueError, KeyError, TypeError) as exc:
        return _failure(frame, str(exc), ctx.locale)
