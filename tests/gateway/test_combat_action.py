import copy

from agent.context import AgentCtx
from agent.services import build_services
from core.character_manager import CharacterSheet
from core.dice_engine import seed_dice
from core.item_model import ItemInstance, load_item_catalog
from core.rulepacks import load_rulepack
from gateway.combat_actions import combat_surface, resolve_action
from infra.config import Settings
from infra.embeddings import FakeEmbeddings
from infra.llm import FakeLLM


async def _scene():
    services = build_services(Settings(), llm=FakeLLM(script=[]), embeddings=FakeEmbeddings(64))
    pack = load_rulepack("dh2")
    catalog = load_item_catalog(pack)
    actor = CharacterSheet("actor", "dh2")
    actor.attributes["BS"] = 90
    weapon = ItemInstance.create(catalog.resolve("lasgun"), state={"current_ammo": 3})
    actor.equipment = [weapon]
    target = CharacterSheet("target", "dh2")
    target.attributes.update({"T": 40, "DAMAGE": 0})
    ctx = AgentCtx(chat_key="cli:dm:combat-action", user_id="u1", locale="en")
    await services.characters.save_character("u1", ctx.chat_key, actor)
    await services.characters.save_character("u2", ctx.chat_key, target)
    return services, ctx, weapon


async def test_single_shot_updates_persistent_sheets_and_emits_same_delta():
    services, ctx, weapon = await _scene()
    surface = await combat_surface(services, ctx, await services.characters.get_character("u1", ctx.chat_key))
    assert surface["actor"] == "actor"
    action = next(a for a in surface["actions"] if a["id"] == "ranged_attack")
    assert "target" in action["targets"]
    assert any(w["id"] == weapon.instance_id for w in action["modes"][0]["weapons"])
    seed_dice(1234)
    result = await resolve_action(services, ctx, {
        "id": "shot-1", "actor": "actor", "target": "target", "action": "ranged_attack",
        "mode": "single", "weapon_instance_id": weapon.instance_id,
    })
    assert result["ok"] is True
    outcome = result["result"]
    assert outcome["ammo_before"] == 3 and outcome["ammo_after"] == 2
    assert outcome["state_delta"]["ammo_after"] == 2
    saved_actor = await services.characters.get_character("u1", ctx.chat_key)
    saved_target = await services.characters.get_character("u2", ctx.chat_key)
    assert saved_actor.equipment[0].current_ammo == outcome["ammo_after"]
    assert saved_target.attributes["DAMAGE"] == outcome["state_delta"]["target_damage_after"]
    assert saved_target.attributes["DAMAGE"] == outcome["final_damage"]
    surface = await combat_surface(services, ctx, saved_actor)
    assert surface["state"]["combatants"]["actor"]["action_budget"] == 1


async def test_invalid_action_does_not_change_any_persistent_state():
    services, ctx, weapon = await _scene()
    before = (
        copy.deepcopy(await services.store.doc_list(ctx.chat_key)),
        copy.deepcopy(await services.store.state_list(ctx.chat_key)),
    )
    result = await resolve_action(services, ctx, {
        "id": "bad", "actor": "actor", "target": "target", "action": "ranged_attack",
        "mode": "single", "weapon_instance_id": "missing",
    })
    assert result["ok"] is False
    assert result["validation_failure"]
    assert (await services.store.doc_list(ctx.chat_key), await services.store.state_list(ctx.chat_key)) == before


async def test_reaction_result_and_reaction_budget_match_persisted_state():
    services, ctx, weapon = await _scene()
    seed_dice(1)
    response = await resolve_action(services, ctx, {
        "id": "dodge", "actor": "actor", "target": "target", "action": "ranged_attack",
        "mode": "single", "weapon_instance_id": weapon.instance_id, "reaction_type": "dodge",
    })
    assert response["ok"] is True
    result = response["result"]
    assert result["reaction"]["type"] == "dodge"
    assert result["state_delta"]["reaction_cost"] == 1
    surface = await combat_surface(services, ctx, await services.characters.get_character("u1", ctx.chat_key))
    assert surface["state"]["combatants"]["target"]["reactions_remaining"] == 0
    assert result["final_damage"] == sum(hit["final_damage"] for hit in result["hits"])


async def test_hidden_sheet_is_not_advertised_or_addressable_as_target():
    services, ctx, weapon = await _scene()
    hidden = CharacterSheet("keeper-secret", "dh2")
    await services.documents.put(ctx.chat_key, "sheet", hidden.name, dict(hidden.to_dict(), owner="keeper"))
    surface = await combat_surface(services, ctx, await services.characters.get_character("u1", ctx.chat_key))
    assert "keeper-secret" not in next(a for a in surface["actions"] if a["id"] == "ranged_attack")["targets"]
    result = await resolve_action(services, ctx, {
        "id": "hidden", "actor": "actor", "target": "keeper-secret", "action": "ranged_attack",
        "mode": "single", "weapon_instance_id": weapon.instance_id,
    })
    assert result["ok"] is False
    assert await services.store.state_get(ctx.chat_key, "combat_state") is None
