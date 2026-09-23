"""Encounter transport: server-authored catalog, turn authority, defender-side reactions.

Characteristic 100 always hits/parries and Agility 0 always fails a Dodge (DH2 has
no automatic-failure band), so outcomes are deterministic without scripting dice.
"""

import copy
import json

from agent import npc as npc_records
from agent.context import AgentCtx
from agent.services import build_services
from core.character_manager import CharacterSheet
from core.combat import COMBAT_STATE_KEY, DECLINE_REACTION, END_TURN_ACTION, REACTION_ACTION
from core.documents import PLAYER_VIEWER
from core.item_model import ItemInstance, load_item_catalog
from core.prompt_sections import inject_game_state_prompt
from core.rulepacks import load_rulepack
from gateway.combat_actions import (
    combat_surface,
    end_room_encounter,
    load_encounter,
    project_action_frame,
    resolve_action,
    should_narrate,
    start_room_encounter,
)
from infra.config import Settings
from infra.embeddings import FakeEmbeddings
from infra.i18n import get_i18n
from infra.llm import FakeLLM

CHAT = "tui:group:combat-encounter"
SECRET = "SECRET-AGENDA-SENTINEL"


def _ctx(user_id: str, role: str) -> AgentCtx:
    return AgentCtx(chat_key=CHAT, user_id=user_id, platform="tui", locale="en", extra={"role": role})


PLAYER = _ctx("u1", "player")
BYSTANDER = _ctx("u2", "player")
KEEPER = _ctx("keeper-1", "keeper")


async def _scene(*, hidden: bool = False, npc_parry: int = 100):
    services = build_services(Settings(locale="en"), llm=FakeLLM(script=[]), embeddings=FakeEmbeddings(64))
    pack = load_rulepack("dh2")
    catalog = load_item_catalog(pack)
    pc = CharacterSheet("Ada", "dh2")
    # AgB 9 -> initiative 10..19; the NPC's AgB 0 -> 1..10; a 10/10 tie goes to higher Agility.
    pc.attributes.update({"BS": 100, "WS": 100, "S": 40, "T": 30, "Ag": 99, "Dodge": 1, "Parry": 1, "DAMAGE": 0})
    pc.equipment = [
        ItemInstance.create(catalog.resolve("lasgun"), state={"current_ammo": 20}),
        ItemInstance.create(catalog.resolve("sword")),
    ]
    await services.characters.save_character("u1", CHAT, pc)
    watcher = CharacterSheet("Bea", "dh2")
    watcher.attributes.update({"Ag": 1, "T": 30})
    await services.characters.save_character("u2", CHAT, watcher)
    record = await npc_records.create_npc(
        services.documents, CHAT, "Cultist", public_description="A hooded cultist.",
        secret_agenda=SECRET, stat_char="Cultist",
    )
    npc = CharacterSheet("Cultist", "dh2")
    npc.attributes.update({"BS": 100, "WS": npc_parry, "S": 30, "T": 30, "Ag": 0, "Dodge": 1, "Parry": 1, "DAMAGE": 0})
    npc.equipment = [
        ItemInstance.create(catalog.resolve("autogun"), state={"current_ammo": 20}),
        ItemInstance.create(catalog.resolve("knife")),
    ]
    await services.documents.put(CHAT, "sheet", "Cultist", dict(npc.to_dict(), owner=f"npc:{record.id}"))
    await start_room_encounter(services, KEEPER, ["Cultist"], hidden={"Cultist"} if hidden else set())
    return services, pc, npc


def _weapon(sheet: CharacterSheet, profile: str) -> str:
    return next(item.instance_id for item in sheet.equipment if item.profile_id == profile)


async def _snapshot(services):
    return (
        copy.deepcopy(await services.store.doc_list(CHAT)),
        copy.deepcopy(await services.store.state_list(CHAT)),
    )


async def _sheet(services, name):
    return CharacterSheet.from_dict(json.loads((await services.store.doc_get(CHAT, "sheet", name))["data"]))


async def test_encounter_start_orders_every_combatant_and_keeps_npcs_off_the_party():
    services, pc, npc = await _scene()
    state = await load_encounter(services, CHAT)
    assert state.order[0] == "Ada" and set(state.order) == {"Ada", "Bea", "Cultist"}
    assert state.combatants["Cultist"].controller == "keeper"
    assert state.combatants["Ada"].controller == "u1"
    roster = {member["name"] for member in await services.characters.get_party_roster(CHAT)}
    assert "Cultist" not in roster
    player = await combat_surface(services, PLAYER, pc)
    assert player["actor"] == "Ada" and player["end_turn"]["id"] == END_TURN_ACTION
    assert "Cultist" in next(a for a in player["actions"] if a["id"] == "ranged_attack")["targets"]
    keeper = await combat_surface(services, KEEPER)
    assert keeper["actor"] == "" and keeper["actions"] == [] and "end_turn" not in keeper


async def test_player_attack_opens_a_keeper_reaction_then_parry_prevents_damage():
    services, pc, npc = await _scene()
    pending = await resolve_action(services, PLAYER, {
        "id": "swing", "actor": "Ada", "target": "Cultist", "action": "melee_attack",
        "mode": "single", "weapon_instance_id": _weapon(pc, "sword"),
    })
    assert pending["ok"] and pending["result"]["pending_reaction"]["choices"] == ["parry", "dodge"]
    assert not should_narrate(pending)
    assert (await _sheet(services, "Cultist")).attributes["DAMAGE"] == 0
    player = await combat_surface(services, PLAYER, pc)
    assert "reaction" not in player and player["actions"] == [] and "end_turn" not in player
    keeper = await combat_surface(services, KEEPER)
    offer = keeper["reaction"]
    assert offer["actor"] == "Cultist" and [c["id"] for c in offer["choices"]] == ["parry", "dodge", DECLINE_REACTION]
    final = await resolve_action(services, KEEPER, {
        "id": "parry", "actor": "Cultist", "action": REACTION_ACTION, "mode": "parry", "pending_id": offer["id"],
    })
    assert final["ok"], final
    assert final["result"]["reaction"]["prevents_hit"] is True and final["result"]["final_damage"] == 0
    assert final["labels"]["reaction"] == "Parry"
    assert should_narrate(final)
    state = await load_encounter(services, CHAT)
    assert state.pending_reaction is None and state.combatants["Cultist"].reactions_remaining == 0


async def test_failed_dodge_applies_damage_to_the_npc_sheet_atomically():
    services, pc, npc = await _scene()
    pending = await resolve_action(services, PLAYER, {
        "id": "shot", "actor": "Ada", "target": "Cultist", "action": "ranged_attack",
        "mode": "single", "weapon_instance_id": _weapon(pc, "lasgun"),
    })
    pending_id = pending["result"]["pending_reaction"]["id"]
    assert (await _sheet(services, "Ada")).equipment[0].current_ammo == 19
    final = await resolve_action(services, KEEPER, {
        "id": "dodge", "actor": "Cultist", "action": REACTION_ACTION, "mode": "dodge", "pending_id": pending_id,
    })
    assert final["ok"] and final["result"]["reaction"]["success"] is False
    damage = final["result"]["final_damage"]
    assert damage == sum(hit["final_damage"] for hit in final["result"]["hits"]) and damage >= 0
    assert (await _sheet(services, "Cultist")).attributes["DAMAGE"] == damage


async def test_npc_attacks_player_on_its_turn_and_the_player_declines():
    services, pc, npc = await _scene()
    state = await load_encounter(services, CHAT)
    for _ in range(3):
        if state.current_actor == "Cultist":
            break
        actor_ctx = PLAYER if state.current_actor == "Ada" else BYSTANDER
        ended = await resolve_action(services, actor_ctx, {
            "id": f"end-{state.current_actor}", "actor": state.current_actor, "action": END_TURN_ACTION,
        })
        assert ended["ok"] and not should_narrate(ended)
        state = await load_encounter(services, CHAT)
    assert state.current_actor == "Cultist"
    keeper = await combat_surface(services, KEEPER)
    assert keeper["actor"] == "Cultist" and "Ada" in keeper["actions"][0]["targets"]
    stab = await resolve_action(services, KEEPER, {
        "id": "stab", "actor": "Cultist", "target": "Ada", "action": "melee_attack",
        "mode": "single", "weapon_instance_id": _weapon(npc, "knife"),
    })
    assert stab["ok"]
    offer = (await combat_surface(services, PLAYER, pc))["reaction"]
    assert offer["attacker"] == "Cultist" and offer["actor"] == "Ada"
    assert "reaction" not in (await combat_surface(services, BYSTANDER))
    final = await resolve_action(services, PLAYER, {
        "id": "take-it", "actor": "Ada", "action": REACTION_ACTION, "mode": DECLINE_REACTION, "pending_id": offer["id"],
    })
    assert final["ok"] and final["result"]["reaction"]["declined"] is True
    saved = await _sheet(services, "Ada")
    assert saved.attributes["DAMAGE"] == final["result"]["final_damage"] > 0
    roster = {member["name"]: member for member in await services.characters.get_party_roster(CHAT)}
    assert any(meter["value"] == saved.attributes["DAMAGE"] for meter in roster["Ada"]["resources"])


async def test_out_of_turn_actions_and_foreign_combatants_are_rejected_without_mutation():
    services, pc, npc = await _scene()
    before = await _snapshot(services)
    attempts = [
        (KEEPER, {"id": "a", "actor": "Cultist", "target": "Ada", "action": "melee_attack",
                  "mode": "single", "weapon_instance_id": _weapon(npc, "knife")}),
        (KEEPER, {"id": "b", "actor": "Ada", "action": END_TURN_ACTION}),
        (BYSTANDER, {"id": "c", "actor": "Ada", "action": END_TURN_ACTION}),
        (BYSTANDER, {"id": "d", "actor": "Bea", "action": END_TURN_ACTION}),
        (PLAYER, {"id": "e", "actor": "Ada", "target": "Cultist", "action": "ranged_attack", "mode": "single",
                  "weapon_instance_id": _weapon(pc, "lasgun"), "reaction_type": "dodge"}),
    ]
    for ctx, frame in attempts:
        refused = await resolve_action(services, ctx, frame)
        assert refused["ok"] is False and refused["validation_failure"], frame
    assert await _snapshot(services) == before
    i18n = get_i18n("en")
    out_of_turn = await resolve_action(services, BYSTANDER, {"id": "f", "actor": "Bea", "action": END_TURN_ACTION})
    assert out_of_turn["validation_failure"] == i18n.t("combat.invalid.turn")


async def test_only_the_defenders_controller_may_answer_and_only_once():
    services, pc, npc = await _scene()
    pending = await resolve_action(services, PLAYER, {
        "id": "shot", "actor": "Ada", "target": "Cultist", "action": "ranged_attack",
        "mode": "single", "weapon_instance_id": _weapon(pc, "lasgun"),
    })
    pending_id = pending["result"]["pending_reaction"]["id"]
    frame = {"id": "r1", "actor": "Cultist", "action": REACTION_ACTION, "mode": "dodge", "pending_id": pending_id}
    before = await _snapshot(services)
    assert (await resolve_action(services, PLAYER, frame))["ok"] is False
    assert (await resolve_action(services, KEEPER, {**frame, "mode": "parry"}))["ok"] is False
    assert await _snapshot(services) == before
    assert (await resolve_action(services, KEEPER, frame))["ok"] is True
    again = await resolve_action(services, KEEPER, {**frame, "id": "r2"})
    assert again["ok"] is False and again["validation_failure"] == get_i18n("en").t("combat.invalid.reaction")
    duplicate = await resolve_action(services, KEEPER, frame)
    assert duplicate["validation_failure"] == get_i18n("en").t("combat.invalid.duplicate")


async def test_second_standard_attack_in_one_turn_is_not_offered_or_accepted():
    services, pc, npc = await _scene(npc_parry=0)
    first = await resolve_action(services, PLAYER, {
        "id": "one", "actor": "Ada", "target": "Cultist", "action": "melee_attack",
        "mode": "single", "weapon_instance_id": _weapon(pc, "sword"),
    })
    await resolve_action(services, KEEPER, {
        "id": "no", "actor": "Cultist", "action": REACTION_ACTION, "mode": DECLINE_REACTION,
        "pending_id": first["result"]["pending_reaction"]["id"],
    })
    surface = await combat_surface(services, PLAYER, pc)
    assert {action["id"] for action in surface["actions"]} <= {"aim", "reload"}
    second = await resolve_action(services, PLAYER, {
        "id": "two", "actor": "Ada", "target": "Cultist", "action": "ranged_attack",
        "mode": "single", "weapon_instance_id": _weapon(pc, "lasgun"),
    })
    assert second["validation_failure"] == get_i18n("en").t("combat.invalid.repeat")


async def test_hidden_npc_never_reaches_a_player_surface_or_result():
    services, pc, npc = await _scene(hidden=True)
    player = await combat_surface(services, PLAYER, pc)
    text = json.dumps(player)
    assert "Cultist" not in text
    refused = await resolve_action(services, PLAYER, {
        "id": "x", "actor": "Ada", "target": "Cultist", "action": "ranged_attack",
        "mode": "single", "weapon_instance_id": _weapon(pc, "lasgun"),
    })
    assert refused["ok"] is False
    keeper = await combat_surface(services, KEEPER)
    assert any(entry["name"] == "Cultist" and entry["hidden"] for entry in keeper["state"]["order"])


async def test_player_grade_action_frame_strips_npc_internals():
    services, pc, npc = await _scene()
    pending = await resolve_action(services, PLAYER, {
        "id": "shot", "actor": "Ada", "target": "Cultist", "action": "ranged_attack",
        "mode": "single", "weapon_instance_id": _weapon(pc, "lasgun"),
    })
    final = await resolve_action(services, KEEPER, {
        "id": "dodge", "actor": "Cultist", "action": REACTION_ACTION, "mode": "dodge",
        "pending_id": pending["result"]["pending_reaction"]["id"],
    })
    public = await project_action_frame(services, CHAT, final, PLAYER_VIEWER)
    assert "combat_state_before" not in public["result"]["state_delta"]
    assert public["result"]["state_delta"]["target_damage_after"] is None
    assert "target" not in public["result"]["reaction"]
    assert public["result"]["final_damage"] == final["result"]["final_damage"]
    assert "combat_state_before" in final["result"]["state_delta"]
    # With no encounter row to consult the projection fails closed.
    await end_room_encounter(services, KEEPER)
    orphan = await project_action_frame(services, CHAT, pending, PLAYER_VIEWER)
    assert orphan["result"]["attack_target"] is None and orphan["result"]["weapon_instance_id"] == ""


async def test_player_owned_sheet_cannot_join_as_a_keeper_npc_and_end_clears_state():
    services, pc, npc = await _scene()
    assert await end_room_encounter(services, KEEPER) is True
    assert await services.store.state_get(CHAT, COMBAT_STATE_KEY) is None
    try:
        await start_room_encounter(services, KEEPER, ["Ada"])
    except Exception as exc:
        assert "not keeper-controlled" in str(exc)
    else:
        raise AssertionError("a player's sheet was accepted as a keeper combatant")
    assert await services.store.state_get(CHAT, COMBAT_STATE_KEY) is None
    refused = await resolve_action(services, PLAYER, {"id": "z", "actor": "Ada", "action": END_TURN_ACTION})
    assert refused["validation_failure"] == get_i18n("en").t("combat.invalid.encounter")


async def test_keeper_prompt_carries_the_engine_encounter():
    services, pc, npc = await _scene(hidden=True)
    text = await inject_game_state_prompt(KEEPER, services.characters, services.store, get_i18n("en"))
    assert "Encounter" in text and "Cultist" in text and "hidden from players" in text
