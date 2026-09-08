from __future__ import annotations

import asyncio
import json

import websockets

from agent.context import AgentCtx
from agent.services import build_services
from gateway.commands import CommandRouter
from gateway.session import SessionSource
from infra.config import Settings
from infra.embeddings import FakeEmbeddings
from infra.llm import FakeLLM
from net.keystore import Keystore, member_id_for_key
from net.localized_session import normalize_client_locale
from net.tui_server import TuiServer

_RECV_TIMEOUT = 5.0


async def _recv(ws) -> dict:
    raw = await asyncio.wait_for(ws.recv(), timeout=_RECV_TIMEOUT)
    return json.loads(raw)


async def _recv_until(ws, frame_type: str) -> dict:
    while True:
        frame = await _recv(ws)
        if frame.get("type") == frame_type:
            return frame


def test_client_locale_normalizes_common_language_tags():
    assert normalize_client_locale(" ru-RU ") == "ru"
    assert normalize_client_locale("zh_CN") == "zh"
    assert normalize_client_locale("EN") == "en"


async def test_locale_frame_relocalizes_the_live_creation_state_without_reconnect():
    services = build_services(Settings(locale="en"), llm=FakeLLM(script=[]), embeddings=FakeEmbeddings(64))
    keystore = Keystore()
    room = "runtime-locale"
    key = keystore.add(room=room, name="Alice", role="player")
    user_id = member_id_for_key(key)
    chat_key = SessionSource(platform="tui", chat_type="group", chat_id=room).chat_key()
    ctx = AgentCtx(chat_key=chat_key, user_id=user_id, platform="tui", locale="en")

    # Seed an active staged creation so the state contains authored rulepack text whose
    # language is unambiguous. This is the exact surface Studio renders in "My character".
    router = CommandRouter(services)
    created = await router.dispatch(ctx, ".dh2 hive_world | Locale Test")
    assert created is not None

    server = TuiServer(services, keystore, port=0)
    await server.start()
    url = f"ws://127.0.0.1:{server.bound_port}/"
    try:
        async with websockets.connect(url) as ws:
            await ws.send(json.dumps({"type": "join", "key": key}))
            welcome = await _recv(ws)
            assert welcome["type"] == "welcome"
            assert welcome["locale"] == "en"

            initial = await _recv_until(ws, "state")
            assert initial["creation"]["stage"]["presentation"]["title"] == "Characteristic reroll"

            # `ru` currently exists in DH2 presentation data even before Loreweaver owns a
            # global locales/ru catalog, so a regional tag must still activate that rulepack text.
            await ws.send(json.dumps({"type": "locale", "locale": "ru-RU"}))
            localized = await _recv_until(ws, "state")
            assert localized["creation"]["stage"]["presentation"]["title"] == "Переброс характеристики"

            # Malformed locale input fails closed instead of being treated as a path/catalog id.
            await ws.send(json.dumps({"type": "locale", "locale": "../ru"}))
            error = await _recv_until(ws, "error")
            assert error["code"] == "bad_frame"
    finally:
        await server.close()
