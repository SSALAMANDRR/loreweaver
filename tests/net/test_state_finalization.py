"""Exercise real creation commands, persistence and the reconnect state surface."""

import asyncio
import copy
import json
from types import SimpleNamespace
from urllib.parse import quote

import pytest
import websockets

from agent.context import AgentCtx
from agent.services import build_services
from core.character_manager import CharacterSheet
from core.creation_flow import start_creation_flow
from core.rulepacks import load_rulepack
from gateway.commands import CommandRouter
from gateway.session import SessionSource
from infra.config import Settings
from infra.embeddings import FakeEmbeddings
from infra.llm import FakeLLM
from net.keystore import Keystore, member_id_for_key
from net.state import build_room_state
from net.tui_server import TuiServer


def _services():
    return build_services(Settings(), llm=FakeLLM(script=[]), embeddings=FakeEmbeddings(64))


def _action(**request):
    return ".__creation_action finalize " + quote(json.dumps({"character": "Acolyte", **request}), safe="")


async def _through_creation(services, router, ctx):
    commands = [
        ".__creation_action start dh2 | feral_world | Acolyte",
        ".__creation_action create done",
        ".__creation_action create Адептус Администратум | trained_skill=Коммерция"
        " | scholastic_lore=Бюрократия | weapon_training=Лазерное | starting_weapon=Лазпистолет | aptitude=Познание",
        ".__creation_action create Хирургеон | role_talent=Нокдаун",
        ".__creation_action create Aptitudes=Навык Стрельбы; Навык Рукопашной",
        ".__creation_action create done",
    ]
    for command in commands:
        reply = await router.dispatch_reply(ctx, command)
        assert reply is not None and not reply.error, (command, reply)
    return await build_room_state(services, ctx)


@pytest.mark.parametrize("roll", [1, 18, 100])
async def test_creation_acquisitions_finalization_and_reconnect(roll, monkeypatch):
    services = _services()
    router = CommandRouter(services)
    ctx = AgentCtx(chat_key=f"cli:dm:finalization-{roll}", user_id="u1", locale="ru")
    state = await _through_creation(services, router, ctx)
    assert state["readiness"]["phase"] == "creation"
    assert "Завершите" in state["readiness"]["message"]
    assert not state["finalization"]["can_roll"]
    assert "result" not in state["finalization"]
    denied = await router.dispatch_reply(ctx, _action(action="roll"))
    assert denied.error
    # The acquisition count and offered items are engine-owned, not inferred by a client.
    while not state["creation"]["complete"]:
        stage = state["creation"]["stage"]
        assert stage["kind"] == "starting_equipment"
        assert stage["budget"]["remaining"] > 0
        item = stage["items"][0]["id"]
        reply = await router.dispatch_reply(ctx, f".__creation_action create {item}")
        assert reply is not None and not reply.error
        state = await build_room_state(services, ctx)
    assert state["readiness"]["ready"] is False
    assert state["finalization"]["can_roll"] is True
    calls = []

    def fixed(expression):
        calls.append(expression)
        return SimpleNamespace(total=roll)

    monkeypatch.setattr(services.dice, "roll_expression", fixed)
    reply = await router.dispatch_reply(ctx, _action(action="roll"))
    assert not reply.error and reply.text == ""
    state = await build_room_state(services, ctx)
    assert state["finalization"]["result"]["roll"] == roll
    assert not state["finalization"]["can_roll"]
    assert calls == ["1d100"]
    if roll == 18:
        assert not state["readiness"]["ready"]
        assert [choice["label"] for choice in state["finalization"]["choices"]] == [
            "Повысить характеристику", "Понизить характеристику",
        ]
        incomplete = await router.dispatch_reply(ctx, _action(action="resolve", roll=roll,
            row_id=state["finalization"]["result"]["id"], selections={"increase": "agility"}))
        assert incomplete.error
        assert (await build_room_state(services, ctx))["finalization"] == state["finalization"]
        reply = await router.dispatch_reply(ctx, _action(action="resolve", roll=roll,
            row_id=state["finalization"]["result"]["id"], selections={"increase": "agility", "decrease": "ballistic_skill"}))
        assert not reply.error
        state = await build_room_state(services, ctx)
    if roll == 1:
        assert state["readiness"]["phase"] == "blocked"
        assert state["readiness"]["blocked_reference"] == "table_8_15_rudiments"
        assert not state["finalization"]["can_resolve"]
        assert not state["readiness"]["ready"]
    else:
        assert state["readiness"]["ready"]
        assert state["finalization"]["complete"]
        assert state["finalization"]["choices"] == []
    # Duplicated/replayed actions cannot roll again, even when the client is stale.
    assert (await router.dispatch_reply(ctx, _action(action="roll"))).error
    reconnect = AgentCtx(chat_key=ctx.chat_key, user_id=ctx.user_id, locale="ru")
    assert (await build_room_state(services, reconnect))["finalization"] == state["finalization"]
    assert calls == ["1d100"]


async def test_missing_choices_remain_visible_and_reading_does_not_mutate():
    services = _services()
    router = CommandRouter(services)
    ctx = AgentCtx(chat_key="cli:dm:required-choices", user_id="u1", locale="ru")
    await router.dispatch(ctx, ".dh2 forge_world | Acolyte")
    await router.dispatch(ctx, ".create done")
    before = (await services.characters.get_character(ctx.user_id, ctx.chat_key)).to_dict()
    state = await build_room_state(services, ctx)
    assert state["creation"]["stage"]["options"][0]["choices"]
    assert state["readiness"]["phase"] == "creation"
    assert not state["finalization"]["can_roll"]
    assert (await services.characters.get_character(ctx.user_id, ctx.chat_key)).to_dict() == before


@pytest.mark.parametrize("system", ["dh2", "coc7", "dnd5e"])
async def test_legacy_sheets_remain_ready_without_forced_finalization(system):
    services = _services()
    ctx = AgentCtx(chat_key=f"cli:dm:legacy-{system}", user_id="u1", locale="ru")
    await services.characters.save_character(ctx.user_id, ctx.chat_key, CharacterSheet("Legacy", system))
    state = await build_room_state(services, ctx)
    assert state["readiness"]["ready"]
    assert not state["readiness"]["managed"]
    assert "finalization" not in state


async def test_invalid_managed_state_fails_closed_and_foreign_actions_do_not_mutate():
    services = _services()
    ctx = AgentCtx(chat_key="cli:dm:invalid-finalization", user_id="u1", locale="ru")
    sheet = start_creation_flow(load_rulepack("dh2"), "feral_world", "Other").character
    sheet.secondary_attributes["__creation_flow__"]["stage_index"] = -1
    await services.characters.save_character(ctx.user_id, ctx.chat_key, sheet)
    state = await build_room_state(services, ctx)
    assert state["readiness"]["phase"] == "invalid"
    assert not state["readiness"]["ready"]
    assert "finalization" not in state
    before = copy.deepcopy(sheet.to_dict())
    assert (await CommandRouter(services).dispatch_reply(ctx, _action(action="roll"))).error
    assert (await services.characters.get_character(ctx.user_id, ctx.chat_key)).to_dict() == before


async def test_live_wire_publishes_finalization_and_restores_it_on_reconnect(monkeypatch):
    services = _services()
    keys = Keystore()
    key = keys.add(room="wire-finalization", name="Player", role="player")
    ctx = AgentCtx(chat_key=SessionSource(platform="tui", chat_type="group", chat_id="wire-finalization").chat_key(),
        user_id=member_id_for_key(key), locale="ru", platform="tui")
    router = CommandRouter(services)
    state = await _through_creation(services, router, ctx)
    while not state["creation"]["complete"]:
        await router.dispatch(ctx, ".create " + state["creation"]["stage"]["items"][0]["id"])
        state = await build_room_state(services, ctx)
    monkeypatch.setattr(services.dice, "roll_expression", lambda expression: SimpleNamespace(total=18))
    server = TuiServer(services, keys, port=0)

    async def next_state(ws):
        async with asyncio.timeout(5):
            while True:
                frame = json.loads(await ws.recv())
                if frame["type"] == "state":
                    return frame

    await server.start()
    try:
        url = f"ws://127.0.0.1:{server.bound_port}/"
        async with websockets.connect(url) as ws:
            await ws.send(json.dumps({"type": "join", "key": key}))
            assert (await next_state(ws))["finalization"]["can_roll"]
            await ws.send(json.dumps({"type": "input", "text": _action(action="roll")}))
            pending = await next_state(ws)
            assert pending["finalization"]["can_resolve"]
            await ws.send(json.dumps({"type": "input", "text": _action(action="resolve", roll=18,
                row_id=pending["finalization"]["result"]["id"], selections={"increase": "agility", "decrease": "ballistic_skill"})}))
            assert (await next_state(ws))["readiness"]["ready"]
        async with websockets.connect(url) as ws:
            await ws.send(json.dumps({"type": "join", "key": key}))
            restored = await next_state(ws)
            assert restored["finalization"]["complete"]
            assert restored["finalization"]["result"]["roll"] == 18
    finally:
        await server.close()
