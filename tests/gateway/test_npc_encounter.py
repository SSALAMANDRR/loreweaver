"""Keeper-created NPCs from pack opponent profiles, through the existing encounter."""

import json

from agent import npc as npc_records
from agent.context import AgentCtx
from agent.services import build_services
from core.character_manager import CharacterSheet
from core.combat import DECLINE_REACTION, END_TURN_ACTION, REACTION_ACTION
from core.item_model import ItemInstance, load_item_catalog
from core.rulepacks import load_rulepack
from gateway.combat_actions import combat_surface, load_encounter, resolve_action, start_room_encounter
from gateway.commands import CommandRouter
from infra.config import Settings
from infra.embeddings import FakeEmbeddings
from infra.i18n import get_i18n
from infra.llm import FakeLLM

CHAT = "tui:group:npc-encounter"


def _ctx(user_id, role):
    return AgentCtx(chat_key=CHAT, user_id=user_id, platform="tui", locale="en", extra={"role": role})


PLAYER, BYSTANDER, KEEPER = _ctx("u1", "player"), _ctx("u2", "player"), _ctx("keeper-1", "keeper")


async def _room():
    services = build_services(
        Settings(locale="en", default_rulepack="dh2"), llm=FakeLLM(script=[]), embeddings=FakeEmbeddings(64)
    )
    catalog = load_item_catalog(load_rulepack("dh2"))
    ada = CharacterSheet("Ada", "dh2")
    ada.attributes.update({"BS": 100, "WS": 100, "S": 40, "T": 30, "Ag": 99, "WOUNDS": 12, "DAMAGE": 0})
    ada.equipment = [ItemInstance.create(catalog.resolve("lasgun"), state={"current_ammo": 60})]
    await services.characters.save_character("u1", CHAT, ada)
    bea = CharacterSheet("Bea", "dh2")
    bea.attributes.update({"Ag": 98, "T": 30, "WOUNDS": 10})
    await services.characters.save_character("u2", CHAT, bea)
    return services, CommandRouter(services), ada


async def _sheet(services, name):
    row = await services.store.doc_get(CHAT, "sheet", name)
    return json.loads(row["data"])


async def test_keeper_lists_profiles_and_creates_an_npc_bound_through_stat_char():
    services, router, _ada = await _room()
    listing = await router.dispatch(KEEPER, ".npc profiles")
    assert "hive_scum" in listing and "Отброс" in listing and "Глава XII" in listing
    created = await router.dispatch(KEEPER, ".npc create hive_scum | Ratgut")
    assert created.startswith("✅"), created
    record = await npc_records.find_npc_by_name(services.documents, CHAT, "Ratgut")
    assert record is not None and record.stat_char == "Ratgut"
    sheet = await _sheet(services, "Ratgut")
    assert sheet["owner"] == f"npc:{record.id}"
    assert sheet["attributes"]["WS"] == 30 and sheet["attributes"]["WOUNDS"] == 9
    assert sheet["fields"]["npc_type"] == "troop"
    roster = {member["name"] for member in await services.characters.get_party_roster(CHAT)}
    assert "Ratgut" not in roster
    assert "ratgut" not in await npc_records.player_character_names(services.documents, CHAT)


async def test_npc_create_refuses_players_bad_profiles_and_name_clashes_without_writing():
    services, router, _ada = await _room()
    assert await router.dispatch(PLAYER, ".npc create hive_scum | Sneak") == get_i18n("en").t("rooms.denied")
    assert await services.store.doc_get(CHAT, "sheet", "Sneak") is None
    assert "No opponent profile" in await router.dispatch(KEEPER, ".npc create ogryn | Big")
    clash = await router.dispatch(KEEPER, ".npc create hive_scum | Ada")
    assert "already used" in clash
    assert (await _sheet(services, "Ada"))["owner"] == "u1"
    assert await npc_records.find_npc_by_name(services.documents, CHAT, "Ada") is None


_CONTROLLER = {"Ada": PLAYER, "Bea": BYSTANDER, "Ratgut": KEEPER}


async def _advance_to(services, actor, tag):
    """End turns in real initiative order until `actor` acts."""
    for step in range(8):
        current = (await load_encounter(services, CHAT)).current_actor
        if current == actor:
            return
        ended = await resolve_action(services, _CONTROLLER[current], {
            "id": f"{tag}-{step}-{current}", "actor": current, "action": END_TURN_ACTION,
        })
        assert ended["ok"], ended
    raise AssertionError(f"{actor} never got a turn")


async def test_created_npc_fights_both_ways_and_a_defeated_troop_leaves_the_turn_order():
    services, router, ada = await _room()
    await router.dispatch(KEEPER, ".npc create hive_scum | Ratgut")
    await start_room_encounter(services, KEEPER, ["Ratgut"])
    assert (await load_encounter(services, CHAT)).combatants["Ratgut"].controller == "keeper"

    # The NPC attacks the player on its own turn; the player declines.
    await _advance_to(services, "Ratgut", "to-npc")
    knife = next(item["instance_id"] for item in (await _sheet(services, "Ratgut"))["equipment"] if item["profile_id"] == "knife")
    stab = await resolve_action(services, KEEPER, {
        "id": "stab", "actor": "Ratgut", "target": "Ada", "action": "melee_attack", "mode": "single",
        "weapon_instance_id": knife,
    })
    assert stab["ok"], stab
    if stab["result"]["pending_reaction"] is not None:
        answered = await resolve_action(services, PLAYER, {
            "id": "take", "actor": "Ada", "action": REACTION_ACTION, "mode": DECLINE_REACTION,
            "pending_id": stab["result"]["pending_reaction"]["id"],
        })
        assert answered["ok"], answered

    # The player shoots the scum each round until Critical Damage takes it out.
    lasgun = ada.equipment[0].instance_id
    defeated = False
    for round_index in range(12):
        await _advance_to(services, "Ada", f"r{round_index}")
        shot = await resolve_action(services, PLAYER, {
            "id": f"shot-{round_index}", "actor": "Ada", "target": "Ratgut", "action": "ranged_attack",
            "mode": "single", "weapon_instance_id": lasgun,
        })
        assert shot["ok"], shot
        final = shot
        if shot["result"]["pending_reaction"] is not None:
            final = await resolve_action(services, KEEPER, {
                "id": f"decline-{round_index}", "actor": "Ratgut", "action": REACTION_ACTION,
                "mode": DECLINE_REACTION, "pending_id": shot["result"]["pending_reaction"]["id"],
            })
            assert final["ok"], final
        if final["result"]["target_defeated"]:
            defeated = True
            break
        ended = await resolve_action(services, PLAYER, {"id": f"end-{round_index}", "actor": "Ada", "action": END_TURN_ACTION})
        assert ended["ok"], ended
    assert defeated
    sheet = await _sheet(services, "Ratgut")
    assert sheet["attributes"]["DAMAGE"] > sheet["attributes"]["WOUNDS"]

    # Out of the fight: not a target, and a full lap of turns never lands on it.
    surface = await combat_surface(services, PLAYER, ada)
    assert all("Ratgut" not in action["targets"] for action in surface["actions"])
    assert next(entry for entry in surface["state"]["order"] if entry["name"] == "Ratgut")["defeated"] is True
    seen = []
    for step in range(4):
        current = (await load_encounter(services, CHAT)).current_actor
        seen.append(current)
        assert (await resolve_action(services, _CONTROLLER[current], {"id": f"lap-{step}", "actor": current, "action": END_TURN_ACTION}))["ok"]
    assert "Ratgut" not in seen and {"Ada", "Bea"} <= set(seen)


async def test_other_players_never_see_the_npc_sheet_values():
    services, router, ada = await _room()
    await router.dispatch(KEEPER, ".npc create desoleum_oathsworn_trooper | Trooper Vex")
    await start_room_encounter(services, KEEPER, ["Trooper Vex"])
    for viewer in (PLAYER, BYSTANDER):
        surface = await combat_surface(services, viewer, ada if viewer is PLAYER else None)
        text = json.dumps(surface, ensure_ascii=False)
        assert "Trooper Vex" in text  # present in the order
        assert "Trooper Vex" not in surface["state"]["combatants"]
        assert "Искусный Стук" not in text and "\"BS\"" not in text
    keeper = await combat_surface(services, KEEPER)
    assert keeper["state"]["combatants"]["Trooper Vex"]["controller"] == "keeper"
