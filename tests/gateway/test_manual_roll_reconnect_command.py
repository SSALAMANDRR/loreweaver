from agent.context import AgentCtx
from agent.services import build_services
from core.character_manager import CharacterSheet
from core.manual_roll import load_pending_roll
from gateway.commands import CommandRouter
from infra.config import Settings
from infra.embeddings import FakeEmbeddings
from infra.llm import FakeLLM


def _services():
    return build_services(Settings(), llm=FakeLLM(script=[]), embeddings=FakeEmbeddings(64))


async def _pending_request(router: CommandRouter, services, ctx: AgentCtx) -> dict:
    sheet = CharacterSheet(name="Acolyte", system="dh2")
    sheet.attributes["Per"] = 42
    sheet.skills["Awareness"] = 1
    await services.characters.save_character(ctx.user_id, ctx.chat_key, sheet)

    mode = await router.dispatch_reply(ctx, ".rollmode manual")
    assert mode is not None and mode.error is False
    requested = await router.dispatch_reply(ctx, ".check hard Awareness")
    assert requested is not None
    return next(
        event.data
        for event in requested.events
        if event.kind == "panel" and event.data.get("type") == "roll_request"
    )


async def test_pending_query_reemits_the_same_private_request_without_consuming_it():
    services = _services()
    router = CommandRouter(services)
    ctx = AgentCtx(chat_key="cli:dm:manual-reconnect", user_id="u1", locale="en")
    original = await _pending_request(router, services, ctx)

    reply = await router.dispatch_reply(ctx, ".__roll_pending")

    assert reply is not None and reply.error is True and reply.text == ""
    event = next(event for event in reply.events if event.kind == "panel")
    assert event.private is True
    assert event.data == original

    persisted = await load_pending_roll(services.store, ctx.chat_key, ctx.user_id)
    assert persisted is not None
    assert persisted.request_id == original["request_id"]


async def test_pending_query_is_silent_when_nothing_is_waiting():
    services = _services()
    router = CommandRouter(services)
    ctx = AgentCtx(chat_key="cli:dm:manual-reconnect-empty", user_id="u1", locale="en")

    reply = await router.dispatch_reply(ctx, ".__roll_pending")

    assert reply is not None and reply.error is True and reply.text == ""
    assert reply.events == ()
