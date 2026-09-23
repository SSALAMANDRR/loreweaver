import json

import websockets

from agent.services import build_services
from core.character_manager import CharacterSheet
from core.dice_engine import seed_dice
from core.item_model import ItemInstance, load_item_catalog
from core.rulepacks import load_rulepack
from gateway.session import SessionSource
from infra.config import Settings
from infra.embeddings import FakeEmbeddings
from infra.llm import FakeLLM, assistant_text
from net.keystore import Keystore, member_id_for_key
from net.tui_server import TuiServer
from tests.net.test_tui_server import _connect_and_join, _recv_until, _start


async def test_action_request_delivers_committed_result_and_same_keeper_context():
    seen = []

    def responder(messages, tools):
        seen.append((messages, tools))
        return assistant_text("The shot cracks across the room.")

    services = build_services(Settings(locale="en"), llm=FakeLLM(responder=responder), embeddings=FakeEmbeddings(64))
    room = "combat-wire"
    chat_key = SessionSource(platform="tui", chat_type="group", chat_id=room).chat_key()
    pack = load_rulepack("dh2")
    catalog = load_item_catalog(pack)
    actor = CharacterSheet("actor", "dh2")
    actor.attributes["BS"] = 90
    weapon = ItemInstance.create(catalog.resolve("lasgun"), state={"current_ammo": 3})
    actor.equipment = [weapon]
    target = CharacterSheet("target", "dh2")
    target.attributes.update({"T": 40, "DAMAGE": 0})
    keystore = Keystore()
    key = keystore.add(room=room, name="Player", role="player")
    # Join identity is the key's stable id, not the caller-supplied display name.
    uid = member_id_for_key(key)
    await services.characters.save_character(uid, chat_key, actor)
    await services.characters.save_character("target-owner", chat_key, target)
    server = TuiServer(services, keystore, port=0)
    url = await _start(server)
    try:
        ws, _, _, state = await _connect_and_join(url, key)
        assert state["combat"]["actor"] == "actor"
        seed_dice(1)
        await ws.send(json.dumps({
            "type": "action_request", "id": "first", "actor": "actor", "target": "target",
            "action": "ranged_attack", "mode": "single", "weapon_instance_id": weapon.instance_id,
        }))
        result = await _recv_until(ws, "action_result")
        assert result["ok"] is True
        assert result["result"]["ammo_after"] == 2
        assert result["result"]["attack_roll"] == 18
        assert result["result"]["hits"]
        assert sum(hit["final_damage"] for hit in result["result"]["hits"]) == result["result"]["final_damage"]
        updated = await _recv_until(ws, "state")
        assert updated["combat"]["state"]["combatants"]["actor"]["action_budget"] == 1
        narrative = await _recv_until(ws, "narrative")
        assert narrative["speaker"] == "kp"
        assert seen and seen[0][1] is None
        prompt = seen[0][0][-1]["content"]
        assert json.dumps(result["result"], ensure_ascii=False) in prompt
        saved = await services.characters.get_character(uid, chat_key)
        assert saved.equipment[0].current_ammo == result["result"]["ammo_after"]
        await ws.send(json.dumps({
            "type": "action_request", "id": "invalid", "actor": "actor", "target": "target",
            "action": "ranged_attack", "mode": "single", "weapon_instance_id": "missing",
        }))
        refused = await _recv_until(ws, "action_result")
        assert refused["ok"] is False and refused["validation_failure"]
        assert (await services.characters.get_character(uid, chat_key)).equipment[0].current_ammo == 2
        await ws.close()
    finally:
        await server.close()
