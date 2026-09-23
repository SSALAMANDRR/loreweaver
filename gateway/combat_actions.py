"""Generic action transport for pack-declared deterministic combat encounters.

Every outbound combat payload goes through `core.combat.project_combat_state` /
`project_combat_result` for the receiving viewer; nothing here serializes raw
combat state onto the wire.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import asdict
from typing import Any

from agent.context import AgentCtx
from agent.services import Services
from core.character_manager import CharacterSheet, character_resources
from core.combat import (
    COMBAT_STATE_KEY,
    DECLINE_REACTION,
    END_TURN_ACTION,
    KEEPER_CONTROLLER,
    REACTION_ACTION,
    ActionRequest,
    CombatantState,
    CombatResult,
    CombatState,
    CombatValidationError,
    EncounterCombatant,
    _action_contract,
    _check_turn_constraints,
    _combat_data,
    accepts_distance,
    apply_combat_state_delta,
    apply_reaction_delta,
    apply_state_delta,
    combat_state_from_json,
    project_combat_result,
    project_combat_state,
    resolve_aim,
    resolve_end_turn,
    resolve_first_shot,
    resolve_melee_attack,
    resolve_reaction,
    resolve_reload,
    start_encounter,
    viewer_controls,
)
from core.documents import Viewer
from core.item_model import ItemInstance, load_item_catalog
from core.manual_roll import ManualRollError, manual_roll_detail, parse_manual_dice_expression
from core.rulepacks import load_rulepack
from infra.i18n import get_i18n
from infra.room_facets import STORAGE_ROOM_STATE, RoomStateFacet

logger = logging.getLogger(__name__)

_RECENT_REQUEST_LIMIT = 64
_PACK_ACTIONS = {"ranged_attack", "melee_attack", "aim", "reload"}
_ATTACK_ACTIONS = {"ranged_attack", "melee_attack"}

ROOM_FACETS = (
    RoomStateFacet(
        name="combat_state",
        owner=__name__,
        reset_scope="story",
        state_keys=frozenset({COMBAT_STATE_KEY}),
        storages=frozenset({STORAGE_ROOM_STATE}),
    ),
)


def _load(raw: str | None) -> tuple[CombatState | None, list[str]]:
    if not raw:
        return None, []
    recent = json.loads(raw).get("recent_requests") or []
    return combat_state_from_json(raw), [str(value) for value in recent]


def _dump(state: CombatState, recent: list[str]) -> str:
    return json.dumps({**asdict(state), "recent_requests": recent[-_RECENT_REQUEST_LIMIT:]}, ensure_ascii=False)


def combat_viewer(ctx: AgentCtx) -> Viewer:
    from gateway.commands.rooms import _is_keeper

    return Viewer(
        role="keeper" if _is_keeper(ctx) else "player",
        member_id=ctx.uid() or None,
        locale=ctx.locale,
    )


def member_viewer(member: Any, locale: str) -> Viewer:
    """The viewer for one hub connection, derived like `gateway.turn.publish_state` does."""
    return combat_viewer(
        AgentCtx(
            chat_key="",
            user_id=str(getattr(member, "state_user_id", None) or getattr(member, "id", "")),
            platform=str(getattr(member, "transport", "") or ""),
            locale=str(getattr(member, "locale", locale) or locale),
            extra={"role": str(getattr(member, "role", "") or "")},
        )
    )


def _label(value: Any, locale: str, fallback: str) -> str:
    if isinstance(value, dict):
        return str(value.get(locale) or value.get("en") or fallback)
    return fallback


def _presentation(pack: Any) -> dict[str, Any]:
    try:
        return dict(_combat_data(pack).get("presentation") or {})
    except CombatValidationError:
        return {}


def _manual_roll_spec(pack: Any, roll_id: str, locale: str) -> dict[str, Any] | None:
    """The physical dice one combat roll needs, in the shared manual-roll wire shape;
    None when the pack's check roll has no manual-safe form."""
    resolver = getattr(pack, "resolver", None)
    if resolver is None:
        return None
    try:
        spec = parse_manual_dice_expression(resolver.roll)
    except ManualRollError:
        return None
    return {"id": roll_id, "label": get_i18n(locale).t(f"combat.manual.{roll_id}"), **spec.wire()}


def _manual_rolls(pack: Any, roll_ids: list[str], locale: str) -> list[dict[str, Any]]:
    specs = [_manual_roll_spec(pack, roll_id, locale) for roll_id in roll_ids]
    return [spec for spec in specs if spec is not None]


def _manual_totals(frame: dict[str, Any], pack: Any, required: list[str]) -> dict[str, int] | None:
    """Validate a request's roll source; for a manual request, turn the submitted natural
    faces for exactly the ``required`` rolls into totals. None means the server rolls."""
    source = frame.get("roll_source", "server")
    submitted = frame.get("manual_rolls")
    if source == "server":
        if submitted not in (None, {}):
            raise CombatValidationError("manual rolls need roll_source manual")  # i18n-exempt: internal validation diagnostic
        return None
    if source != "manual" or not isinstance(submitted, dict) or set(submitted) != set(required):
        raise CombatValidationError("manual roll does not match the requested dice")  # i18n-exempt: internal validation diagnostic
    totals: dict[str, int] = {}
    for roll_id in required:
        spec = _manual_roll_spec(pack, roll_id, "en")
        faces = submitted[roll_id]
        # JSON integers only: the shared validator would also coerce "57" or true.
        if spec is None or not isinstance(faces, list) or not all(type(face) is int for face in faces):
            raise CombatValidationError("manual roll does not match the requested dice")  # i18n-exempt: internal validation diagnostic
        try:
            totals[roll_id] = manual_roll_detail(spec["expression"], faces).total
        except ManualRollError as exc:
            raise CombatValidationError("manual roll does not match the requested dice") from exc  # i18n-exempt: internal validation diagnostic
    return totals


async def _sheet(services: Services, chat_key: str, name: str) -> tuple[dict[str, Any], CharacterSheet]:
    row = await services.store.doc_get(chat_key, "sheet", name)
    if row is None:
        raise CombatValidationError("target is unavailable")
    return row, CharacterSheet.from_dict(json.loads(row["data"]))


def _owner(row: dict[str, Any]) -> str:
    owner = json.loads(row["data"]).get("owner")
    return owner if isinstance(owner, str) else ""


def available_actions(
    pack: Any, actor: CharacterSheet, targets: list[str], locale: str, state: CombatState | None = None
) -> list[dict[str, Any]]:
    """Expose only modes the pack, item profile, inventory and turn state allow now."""
    try:
        data = _combat_data(pack)
        catalog = load_item_catalog(pack)
    except (CombatValidationError, ValueError):
        return []
    presentation = data.get("presentation") or {}
    actor_state = state.combatants.get(actor.name) if state is not None else None
    if state is not None and (
        state.current_actor != actor.name or actor_state is None or state.pending_reaction is not None
    ):
        return []
    entries: list[dict[str, Any]] = []
    for action, definition in (data.get("actions") or {}).items():
        modes: list[dict[str, Any]] = []
        for mode, contract in (definition.get("modes") or {}).items():
            if actor_state is not None:
                if actor_state.action_budget < contract.get("action_cost", 0):
                    continue
                try:
                    _check_turn_constraints(pack, action, _action_contract(pack, action, mode), state, actor_state)
                except CombatValidationError:
                    continue
            weapons: list[dict[str, str]] = []
            for item in actor.equipment:
                if not isinstance(item, ItemInstance):
                    continue
                profile = catalog.get(item.profile_id)
                if profile is None or profile.kind != "weapon" or not profile.is_usable_for_resolution:
                    continue
                if action in _ATTACK_ACTIONS and profile.id not in contract.get("weapon_profiles", []):
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
            if weapons and (action not in _ATTACK_ACTIONS or targets):
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
                    "manual_rolls": _manual_rolls(pack, ["attack"] if action in _ATTACK_ACTIONS else [], locale),
                    "accepts_distance": accepts_distance(action),
                })
        if modes:
            entries.append({
                "id": action,
                "label": _label((presentation.get("actions") or {}).get(action), locale, action),
                "modes": modes,
                "targets": targets if action in _ATTACK_ACTIONS else [],
            })
    return entries


def _reaction_offer(
    state: CombatState, viewer: Viewer, pack: Any, locale: str
) -> dict[str, Any] | None:
    pending = state.pending_reaction
    if pending is None:
        return None
    defender = state.combatants.get(str(pending.get("defender")))
    if defender is None or not viewer_controls(defender, viewer):
        return None
    view = project_combat_state(state, viewer)["pending_reaction"] or {}
    presentation = _presentation(pack)
    reactions = presentation.get("reactions") or {}
    choices = [
        {
            "id": kind,
            "label": _label(reactions.get(kind), locale, kind),
            "manual_rolls": _manual_rolls(pack, ["reaction"], locale),
        }
        for kind in pending.get("choices") or []
    ]
    choices.append(
        {"id": DECLINE_REACTION, "label": get_i18n(locale).t("combat.reaction.decline"), "manual_rolls": []}
    )
    action = str(pending.get("action") or "")
    return {
        "id": pending["id"],
        "actor": pending["defender"],
        "attacker": view.get("attacker", ""),
        "action": _label((presentation.get("actions") or {}).get(action), locale, action),
        "hit_count": pending.get("hit_count"),
        "choices": choices,
    }


async def combat_surface(
    services: Services, ctx: AgentCtx, own_sheet: CharacterSheet | None = None
) -> dict[str, Any] | None:
    """The viewer's projected encounter plus the choices the server allows them now."""
    try:
        state, _recent = _load(await services.store.state_get(ctx.chat_key, COMBAT_STATE_KEY))
        if state is None:
            return None
        viewer = combat_viewer(ctx)
        surface: dict[str, Any] = {
            "actor": "",
            "actions": [],
            "state": project_combat_state(state, viewer),
        }
        current = state.combatants.get(state.current_actor)
        pack = None
        if current is not None and viewer_controls(current, viewer):
            _row, actor = await _sheet(services, ctx.chat_key, state.current_actor)
            pack = load_rulepack(actor.system)
            targets = [
                name for name in state.order
                if name != actor.name
                and not state.combatants[name].defeated
                and (viewer.is_keeper or not state.combatants[name].hidden)
            ]
            surface["actor"] = actor.name
            surface["actions"] = available_actions(pack, actor, targets, ctx.locale, state)
            if state.pending_reaction is None:
                surface["end_turn"] = {
                    "id": END_TURN_ACTION,
                    "label": get_i18n(ctx.locale).t("combat.control.end_turn"),
                }
        elif own_sheet is not None and own_sheet.name in state.combatants:
            surface["actor"] = own_sheet.name
        if state.pending_reaction is not None:
            if pack is None:
                _row, defender = await _sheet(services, ctx.chat_key, str(state.pending_reaction["defender"]))
                pack = load_rulepack(defender.system)
            offer = _reaction_offer(state, viewer, pack, ctx.locale)
            if offer is not None:
                surface["reaction"] = offer
        return surface
    except (CombatValidationError, ValueError, KeyError, TypeError):
        return None


_FAILURE_KEYS = (
    ("manual roll", "manual_roll"),
    ("duplicate", "duplicate"),
    ("no matching reaction", "reaction"),
    ("no active encounter", "encounter"),
    ("pending", "pending"),
    ("current turn", "turn"),
    ("own turn", "turn"),
    ("already taken", "repeat"),
    ("active character", "actor"),
    ("controlled by", "actor"),
    ("owned by", "actor"),
    ("not a combatant", "actor"),
    ("target", "target"),
    ("weapon", "weapon"),
    ("ammunition", "ammo"),
    ("distance", "distance"),
    ("action budget", "budget"),
    ("reaction", "reaction"),
    ("stale", "stale"),
)


def _failure(frame: dict[str, Any], reason: str, locale: str) -> dict[str, Any]:
    key = "combat.invalid"
    for fragment, suffix in _FAILURE_KEYS:
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


def _short_str(frame: dict[str, Any], key: str, *, required: bool = True) -> str:
    value = frame.get(key)
    if value is None and not required:
        return ""
    if not isinstance(value, str) or not 0 < len(value) <= 120:
        raise CombatValidationError(f"{key} is required")  # i18n-exempt: internal validation diagnostic
    return value


def _sheet_update(row: dict[str, Any], sheet: CharacterSheet) -> tuple[str, str, str, str]:
    data = dict(sheet.to_dict(), owner=_owner(row))
    meta = json.loads(row["meta"])
    meta["modified"] = time.time()
    return sheet.name, row["data"], json.dumps(data, ensure_ascii=False), json.dumps(meta, ensure_ascii=False)


async def _commit(
    services: Services,
    chat_key: str,
    *,
    raw_state: str | None,
    state: CombatState,
    recent: list[str],
    sheets: list[tuple[dict[str, Any], CharacterSheet]],
) -> None:
    old_roster = await services.store.state_get(chat_key, "party_roster")
    roster = json.loads(old_roster) if old_roster else {}
    state_rows = [(COMBAT_STATE_KEY, raw_state, _dump(state, recent))]
    touched = [sheet for _row, sheet in sheets if sheet.name in roster]
    for sheet in touched:
        roster[sheet.name]["resources"] = character_resources(sheet)
    if touched:
        state_rows.append(("party_roster", old_roster, json.dumps(roster, ensure_ascii=False)))
    documents = [_sheet_update(row, sheet) for row, sheet in sheets]
    if not await services.store.commit_combat_rows(chat_key, documents=documents, state=state_rows):
        raise CombatValidationError("stale combat state")


def _labels(pack: Any, result: CombatResult, locale: str) -> dict[str, Any]:
    presentation = _presentation(pack)
    if result.action == END_TURN_ACTION:
        return {
            "action": get_i18n(locale).t("combat.control.end_turn"),
            "mode": "", "weapon": "", "locations": {}, "reaction": "",
        }
    catalog = load_item_catalog(pack)
    profile = catalog.get(result.weapon_profile_id) if catalog is not None else None
    reaction_kind = str((result.reaction or {}).get("type") or "")
    reaction_label = (
        get_i18n(locale).t("combat.reaction.decline")
        if reaction_kind == DECLINE_REACTION
        else _label((presentation.get("reactions") or {}).get(reaction_kind), locale, reaction_kind)
    )
    return {
        "action": _label((presentation.get("actions") or {}).get(result.action), locale, result.action),
        "mode": _label((presentation.get("modes") or {}).get(result.mode), locale, result.mode),
        "weapon": profile.name if profile is not None else result.weapon_profile_id,
        "locations": {
            hit.location: _label((presentation.get("locations") or {}).get(hit.location), locale, hit.location)
            for hit in result.hits
        },
        "reaction": reaction_label if reaction_kind else "",
    }


async def resolve_action(services: Services, ctx: AgentCtx, frame: dict[str, Any]) -> dict[str, Any]:
    """Validate, resolve and commit one client action; return the keeper-grade wire result.

    Callers must project the result per viewer (`project_action_frame`) before
    sending it anywhere but a keeper.
    """
    try:
        request_id = _short_str(frame, "id", required=False)
        action = _short_str(frame, "action")
        actor_name = _short_str(frame, "actor")
        raw_state = await services.store.state_get(ctx.chat_key, COMBAT_STATE_KEY)
        state, recent = _load(raw_state)
        if state is None:
            raise CombatValidationError("no active encounter")  # i18n-exempt: internal validation diagnostic
        if request_id and request_id in recent:
            raise CombatValidationError("duplicate request")  # i18n-exempt: internal validation diagnostic
        viewer = combat_viewer(ctx)
        combatant = state.combatants.get(actor_name)
        if combatant is None:
            raise CombatValidationError("actor is not a combatant")  # i18n-exempt: internal validation diagnostic
        if not viewer_controls(combatant, viewer):
            raise CombatValidationError("actor is not controlled by the caller")  # i18n-exempt: internal validation diagnostic
        actor_row, actor = await _sheet(services, ctx.chat_key, actor_name)
        owner = _owner(actor_row)
        if combatant.controller != KEEPER_CONTROLLER and owner != ctx.uid():
            raise CombatValidationError("actor is not owned by the caller")  # i18n-exempt: internal validation diagnostic
        pack = load_rulepack(actor.system)
        recent = [*recent, request_id] if request_id else recent
        sheets: list[tuple[dict[str, Any], CharacterSheet]] = []

        if action == END_TURN_ACTION:
            if state.current_actor != actor_name:
                raise CombatValidationError("actor does not have the current turn")  # i18n-exempt: internal validation diagnostic
            _manual_totals(frame, pack, [])
            delta = resolve_end_turn(state, pack=pack)
            apply_combat_state_delta(state, delta)
            result = CombatResult(actor_name, "", END_TURN_ACTION, "", "", state_delta=delta)
        elif action == REACTION_ACTION:
            pending = state.pending_reaction
            choice = _short_str(frame, "mode")
            if pending is None or pending.get("id") != _short_str(frame, "pending_id"):
                raise CombatValidationError("no matching reaction is pending")  # i18n-exempt: internal validation diagnostic
            if pending.get("defender") != actor_name:
                raise CombatValidationError("actor is not controlled by the caller")  # i18n-exempt: internal validation diagnostic
            manual = _manual_totals(frame, pack, [] if choice == DECLINE_REACTION else ["reaction"])
            _attacker_row, attacker = await _sheet(services, ctx.chat_key, str(pending["attacker"]))
            result = resolve_reaction(
                pending_id=str(pending["id"]), choice=choice, attacker=attacker, defender=actor,
                combat_state=state, pack=pack,
                reaction_roll=(manual or {}).get("reaction"),
                reaction_source="manual" if manual else None,
            )
            if not result.ok:
                raise CombatValidationError(result.validation_failure or "invalid reaction")
            apply_reaction_delta(actor, result, combat_state=state, pack=pack)
            sheets.append((actor_row, actor))
        elif action in _PACK_ACTIONS:
            mode = _short_str(frame, "mode")
            weapon_id = _short_str(frame, "weapon_instance_id")
            if frame.get("reaction_type") is not None:
                raise CombatValidationError("the attacker cannot choose the defender's reaction")  # i18n-exempt: internal validation diagnostic
            distance = frame.get("distance")
            if distance is not None and (type(distance) is not int or distance < 0):
                raise CombatValidationError("invalid distance")
            manual = _manual_totals(frame, pack, ["attack"] if action in _ATTACK_ACTIONS else [])
            target_name = _short_str(frame, "target", required=action in _ATTACK_ACTIONS)
            target_row = target = None
            if target_name:
                target_state = state.combatants.get(target_name)
                if target_state is None or (target_state.hidden and not viewer.is_keeper):
                    raise CombatValidationError("target is unavailable")
                target_row, target = await _sheet(services, ctx.chat_key, target_name)
            request = ActionRequest(
                actor=actor, target=target, weapon_instance_id=weapon_id, mode=mode, distance=distance,
                attack_roll=(manual or {}).get("attack"),
                roll_sources={"attack": "manual"} if manual and "attack" in manual else {},
            )
            if action in _ATTACK_ACTIONS:
                resolver = resolve_first_shot if action == "ranged_attack" else resolve_melee_attack
                result = resolver(
                    request, combat_state=state, pack=pack, reaction_window=True, pending_id=uuid.uuid4().hex,
                )
            else:
                result = (resolve_aim if action == "aim" else resolve_reload)(request, combat_state=state, pack=pack)
            if not result.ok:
                raise CombatValidationError(result.validation_failure or "invalid action")
            apply_state_delta(request, result, combat_state=state, pack=pack)
            sheets.append((actor_row, actor))
            if target_row is not None and target is not None:
                sheets.append((target_row, target))
        else:
            raise CombatValidationError("unknown action")

        await _commit(services, ctx.chat_key, raw_state=raw_state, state=state, recent=recent, sheets=sheets)
        return {
            "type": "action_result",
            "id": request_id,
            "ok": True,
            "result": asdict(result),
            "labels": _labels(pack, result, ctx.locale),
            "validation_failure": None,
        }
    except (CombatValidationError, ValueError, KeyError, TypeError) as exc:
        await _trace_rejection(services, ctx, frame, exc)
        return _failure(frame, str(exc), ctx.locale)


_TRACED_FIELDS = (
    "id", "actor", "action", "mode", "weapon_instance_id", "target", "distance",
    "pending_id", "roll_source", "manual_rolls",
)


async def _trace_rejection(services: Services, ctx: AgentCtx, frame: dict[str, Any], exc: Exception) -> None:
    """Record why a request was refused, next to what the server offers this viewer from
    the same (unchanged) state, so an offered-but-refused action is diagnosable."""
    try:
        state, _recent = _load(await services.store.state_get(ctx.chat_key, COMBAT_STATE_KEY))
        actor = state.combatants.get(str(frame.get("actor"))) if state is not None else None
        surface = await combat_surface(services, ctx)
        offered = [
            {
                "action": action["id"],
                "modes": [
                    {"mode": mode["id"], "weapons": [w["id"] for w in mode["weapons"]],
                     "accepts_distance": mode.get("accepts_distance")}
                    for mode in action["modes"]
                ],
                "targets": action["targets"],
            }
            for action in (surface or {}).get("actions", [])
        ]
        logger.info(
            "combat action rejected: %s",
            json.dumps(
                {
                    "room": ctx.chat_key,
                    "error": f"{type(exc).__name__}: {exc}",
                    "request": {key: frame.get(key) for key in _TRACED_FIELDS if key in frame},
                    "current_actor": state.current_actor if state is not None else None,
                    "round": state.round_number if state is not None else None,
                    "pending_reaction": (
                        {key: state.pending_reaction.get(key) for key in ("id", "attacker", "defender", "choices")}
                        if state is not None and state.pending_reaction else None
                    ),
                    "actor_budget": actor.action_budget if actor is not None else None,
                    "actor_turn_actions": list(actor.turn_actions) if actor is not None else None,
                    "surface_actor": (surface or {}).get("actor"),
                    "surface_actions": offered,
                },
                ensure_ascii=False,
            ),
        )
    except Exception:  # noqa: BLE001 - a diagnostic must never change the refusal
        logger.exception("combat action rejected (trace failed): %s", exc)


async def project_action_frame(
    services: Services, chat_key: str, frame: dict[str, Any], viewer: Viewer
) -> dict[str, Any]:
    """One viewer's copy of a committed `action_result` frame, projected against the
    encounter as committed."""
    if not frame.get("ok") or not isinstance(frame.get("result"), dict):
        return dict(frame)
    state, _recent = _load(await services.store.state_get(chat_key, COMBAT_STATE_KEY))
    if state is None:
        # Fail closed: with no encounter to consult, mask both sides as keeper-side.
        names = {str(frame["result"].get(key) or "") for key in ("actor", "target")} - {""}
        state = CombatState(1, "", {name: CombatantState(0, 0, 0, 0, controller=KEEPER_CONTROLLER) for name in names})
    return {**frame, "result": project_combat_result(frame["result"], state, viewer)}


def should_narrate(frame: dict[str, Any]) -> bool:
    """Only a fully resolved pack action is narrated; turn passes and open reaction
    windows have no fictional outcome yet."""
    result = frame.get("result") if frame.get("ok") else None
    return (
        isinstance(result, dict)
        and result.get("action") != END_TURN_ACTION
        and result.get("pending_reaction") is None
    )


# --- Keeper encounter control ----------------------------------------------------------------


def _is_player_owner(owner: str) -> bool:
    from agent.npc import COMPANION_UID_PREFIX, NPC_UID_PREFIX

    return bool(owner) and not owner.startswith((NPC_UID_PREFIX, COMPANION_UID_PREFIX))


async def start_room_encounter(
    services: Services, ctx: AgentCtx, npc_names: list[str], *, hidden: set[str] | None = None
) -> tuple[CombatState, list[Any]]:
    """Open an encounter with every player character of the room's system plus the named
    keeper-side combatants, rolling initiative server-side. Raises CombatValidationError.

    A character sheet owned by a member is a player character whoever that member is:
    a keeper playing their own PC (solo play) fights under their own member id, exactly
    like any player. Keeper-side combatants are only sheets no member owns (`npc:`).
    """
    from agent import npc as npc_records

    raw_state = await services.store.state_get(ctx.chat_key, COMBAT_STATE_KEY)
    if raw_state:
        raise CombatValidationError("an encounter is already active")  # i18n-exempt: internal validation diagnostic
    members: list[EncounterCombatant] = []
    rows: dict[str, dict[str, Any]] = {}
    for requested in npc_names:
        record = await npc_records.get_npc(services.documents, ctx.chat_key, requested)
        sheet_name = (record.stat_char or record.name) if record is not None else requested
        row, sheet = await _sheet(services, ctx.chat_key, sheet_name)
        if _is_player_owner(_owner(row)):
            raise CombatValidationError(f"combatant {sheet_name!r} is not keeper-controlled")  # i18n-exempt: internal validation diagnostic
        if sheet.name in rows:
            continue
        rows[sheet.name] = row
        members.append(EncounterCombatant(sheet, KEEPER_CONTROLLER, sheet.name in (hidden or set())))
    npc_system = members[0].sheet.system if members else ""
    for member in await services.characters.get_party_roster(ctx.chat_key):
        name = str(member.get("name") or "")
        if not name or name in rows:
            continue
        row = await services.store.doc_get(ctx.chat_key, "sheet", name)
        if row is None:
            continue
        owner = _owner(row)
        sheet = CharacterSheet.from_dict(json.loads(row["data"]))
        if not _is_player_owner(owner) or (npc_system and sheet.system != npc_system):
            continue
        rows[sheet.name] = row
        members.append(EncounterCombatant(sheet, owner))
    if len(members) < 2:
        raise CombatValidationError("an encounter needs at least two uniquely named combatants")  # i18n-exempt: internal validation diagnostic
    pack = load_rulepack(members[0].sheet.system)
    state, rolls = start_encounter(members, pack=pack)
    # Keeper-side stat sheets are not party members: they leave the public roster.
    old_roster = await services.store.state_get(ctx.chat_key, "party_roster")
    roster = json.loads(old_roster) if old_roster else {}
    state_rows = [(COMBAT_STATE_KEY, None, _dump(state, []))]
    npc_in_roster = [member.sheet.name for member in members if member.controller == KEEPER_CONTROLLER and member.sheet.name in roster]
    if npc_in_roster:
        for name in npc_in_roster:
            roster.pop(name, None)
        state_rows.append(("party_roster", old_roster, json.dumps(roster, ensure_ascii=False)))
    if not await services.store.commit_combat_rows(ctx.chat_key, documents=[], state=state_rows):
        raise CombatValidationError("stale combat state")
    return state, rolls


async def end_room_encounter(services: Services, ctx: AgentCtx) -> bool:
    raw_state = await services.store.state_get(ctx.chat_key, COMBAT_STATE_KEY)
    if not raw_state:
        return False
    return await services.store.commit_combat_rows(
        ctx.chat_key, documents=[], state=[(COMBAT_STATE_KEY, raw_state, None)]
    )


async def set_combatant_hidden(services: Services, ctx: AgentCtx, name: str, hidden: bool) -> bool:
    raw_state = await services.store.state_get(ctx.chat_key, COMBAT_STATE_KEY)
    state, recent = _load(raw_state)
    if state is None or name not in state.combatants or state.combatants[name].controller != KEEPER_CONTROLLER:
        return False
    state.combatants[name].hidden = hidden
    return await services.store.commit_combat_rows(
        ctx.chat_key, documents=[], state=[(COMBAT_STATE_KEY, raw_state, _dump(state, recent))]
    )


async def load_encounter(services: Services, chat_key: str) -> CombatState | None:
    state, _recent = _load(await services.store.state_get(chat_key, COMBAT_STATE_KEY))
    return state
