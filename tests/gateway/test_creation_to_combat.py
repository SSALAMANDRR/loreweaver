"""Creation → persist → combat, end to end.

Creation and combat were each tested in isolation, but a character created through
the real staged flow reached its turn with no combat actions: background equipment
was stored as plain labels, which the combat catalog cannot use. This walks the
actual path (background weapon package, acquisitions, finalization, reload from
storage, canonical NPC, encounter) and asserts the PC can fight with what it chose.
"""

import json
from types import SimpleNamespace

import pytest

from agent.context import AgentCtx
from agent.services import build_services
from core.character_manager import CharacterSheet
from core.combat import END_TURN_ACTION
from gateway.combat_actions import combat_surface, load_encounter, resolve_action
from gateway.commands import CommandRouter
from infra.config import Settings
from infra.embeddings import FakeEmbeddings
from infra.llm import FakeLLM

CHAT = "tui:group:creation-to-combat"
KEEPER = AgentCtx(chat_key=CHAT, user_id="keeper-1", platform="tui", locale="en", extra={"role": "keeper"})
PLAYER = AgentCtx(chat_key=CHAT, user_id="u1", platform="tui", locale="en", extra={"role": "player"})

CREATION = [
    ".dh2 Мир-улей | Кардел",
    ".create done",
    ".create Астра Милитарум | trained_skill=Медика | weapon_package=Лазган | aptitude=Полевое",
    ".create Хирургеон | role_talent=Нокдаун",
    ".create Aptitudes=Навык Стрельбы",
    ".create done",
]
# Acquisitions = Influence Bonus (rolled); the knife first, then fillers the item
# catalog does not know, until the budget is spent.
ACQUISITIONS = [".create Нож", ".create Цепной клинок", ".create Стаб-револьвер", ".create Медпакет", ".create Лук"]


@pytest.mark.parametrize("creator", [KEEPER, PLAYER], ids=["keeper-solo-pc", "player-pc"])
async def test_a_character_created_through_the_real_flow_can_attack_on_its_turn(creator):
    services = build_services(
        Settings(locale="en", default_rulepack="dh2"), llm=FakeLLM(script=[]), embeddings=FakeEmbeddings(64)
    )
    router = CommandRouter(services)
    for command in CREATION:
        reply = await router.dispatch(creator, command)
        assert reply is not None and "cannot be applied" not in reply, (command, reply)
    for command in ACQUISITIONS:
        reply = await router.dispatch(creator, command)
        assert "Added" in reply, (command, reply)
        if "choices are complete" in reply:
            break
    else:
        raise AssertionError("starting acquisitions never completed")
    services.dice.roll_expression = lambda expression: SimpleNamespace(total=100)
    assert "ready for play" in await router.dispatch(creator, ".finalize roll")

    # Persisted, not merely upgraded on read: the stored row holds typed items.
    stored = json.loads((await services.store.doc_get(CHAT, "sheet", "Кардел"))["data"])
    typed = {entry["profile_id"]: entry for entry in stored["equipment"] if isinstance(entry, dict)}
    assert typed["lasgun"]["state"]["current_ammo"] == 60  # background weapon package
    assert "knife" in typed  # starting acquisition
    assert "Цепной клинок" in stored["equipment"]  # not in the item catalog: stays a label
    kardel = CharacterSheet.from_dict(stored)

    assert (await router.dispatch(KEEPER, ".npc create hive_scum | Культист")).startswith("✅")
    assert not (await router.dispatch(KEEPER, ".combat start Культист")).startswith("❌")
    for step in range(3):
        current = (await load_encounter(services, CHAT)).current_actor
        if current == "Кардел":
            break
        assert (await resolve_action(services, KEEPER, {"id": f"npc-{step}", "actor": current, "action": END_TURN_ACTION}))["ok"]

    surface = await combat_surface(services, creator, kardel)
    assert surface["actor"] == "Кардел"
    actions = {action["id"]: action for action in surface["actions"]}
    assert actions, "the PC's turn offers no combat actions"
    lasgun_id = typed["lasgun"]["instance_id"]
    ranged_weapons = {weapon["id"] for mode in actions["ranged_attack"]["modes"] for weapon in mode["weapons"]}
    assert lasgun_id in ranged_weapons and "Культист" in actions["ranged_attack"]["targets"]
    melee_weapons = {weapon["id"] for mode in actions["melee_attack"]["modes"] for weapon in mode["weapons"]}
    assert typed["knife"]["instance_id"] in melee_weapons

    shot = await resolve_action(services, creator, {
        "id": "first-shot", "actor": "Кардел", "target": "Культист", "action": "ranged_attack",
        "mode": "single", "weapon_instance_id": lasgun_id,
    })
    assert shot["ok"], shot
