from __future__ import annotations

from agent.context import AgentCtx
from agent.services import build_services
from core.character_manager import CharacterSheet
from core.dice_engine import seed_dice
from core.manual_roll import load_pending_roll
from gateway.commands import CommandRouter
from infra.config import Settings
from infra.embeddings import FakeEmbeddings
from infra.llm import FakeLLM


def _services():
    return build_services(Settings(), llm=FakeLLM(script=[]), embeddings=FakeEmbeddings(64))


async def _dh2_character(services, ctx: AgentCtx) -> CharacterSheet:
    sheet = CharacterSheet(name="Acolyte", system="dh2")
    sheet.attributes["Per"] = 42
    sheet.skills["Awareness"] = 1
    await services.characters.save_character(ctx.user_id, ctx.chat_key, sheet)
    return sheet


async def _manual_request(router: CommandRouter, ctx: AgentCtx):
    mode = await router.dispatch_reply(ctx, ".rollmode manual")
    assert mode is not None and mode.error is False

    requested = await router.dispatch_reply(ctx, ".check hard Awareness")
    assert requested is not None
    assert requested.error is True
    assert requested.text == ""
    request = next(event for event in requested.events if event.kind == "panel")
    assert request.private is True
    assert request.data["type"] == "roll_request"
    return request.data


async def test_auto_mode_keeps_the_existing_check_lane_unchanged():
    services = _services()
    router = CommandRouter(services)
    ctx = AgentCtx(chat_key="cli:dm:manual-auto", user_id="u1", locale="en")
    await _dh2_character(services, ctx)

    seed_dice(7)
    reply = await router.dispatch_reply(ctx, ".check hard Awareness")

    assert reply is not None and reply.error is False
    assert len(reply.events) == 1
    event = reply.events[0]
    assert event.kind == "dice"
    assert event.data["kind"] == "check"
    assert event.data["target"] == 42
    assert event.data["effective_target"] == 22
    assert event.data.get("detail", {}).get("source") is None
    assert await load_pending_roll(services.store, ctx.chat_key, ctx.user_id) is None


async def test_manual_mode_persists_private_d100_request_with_difficulty_and_target():
    services = _services()
    router = CommandRouter(services)
    ctx = AgentCtx(chat_key="cli:dm:manual-request", user_id="u1", locale="en")
    await _dh2_character(services, ctx)

    request = await _manual_request(router, ctx)

    assert request["kind"] == "check"
    assert request["reason"] == "Awareness"
    assert request["expression"] == "1d100"
    assert request["count"] == 1
    assert request["sides"] == 100
    assert request["target"] == 42
    assert request["effective_target"] == 22
    assert request["difficulty"] == "hard"

    pending = await load_pending_roll(services.store, ctx.chat_key, ctx.user_id)
    assert pending is not None
    assert pending.request_id == request["request_id"]


async def test_manual_submit_rejects_wrong_request_without_consuming_pending():
    services = _services()
    router = CommandRouter(services)
    ctx = AgentCtx(chat_key="cli:dm:manual-wrong-id", user_id="u1", locale="en")
    await _dh2_character(services, ctx)
    request = await _manual_request(router, ctx)

    reply = await router.dispatch_reply(ctx, ".__roll_submit wrong 17")

    assert reply is not None and reply.error is True
    assert reply.events == ()
    pending = await load_pending_roll(services.store, ctx.chat_key, ctx.user_id)
    assert pending is not None and pending.request_id == request["request_id"]


async def test_manual_submit_rejects_out_of_range_face_without_consuming_pending():
    services = _services()
    router = CommandRouter(services)
    ctx = AgentCtx(chat_key="cli:dm:manual-bad-face", user_id="u1", locale="en")
    await _dh2_character(services, ctx)
    request = await _manual_request(router, ctx)

    reply = await router.dispatch_reply(
        ctx, f".__roll_submit {request['request_id']} 101"
    )

    assert reply is not None and reply.error is True
    assert not any(event.kind == "dice" for event in reply.events)
    pending = await load_pending_roll(services.store, ctx.chat_key, ctx.user_id)
    assert pending is not None and pending.request_id == request["request_id"]


async def test_valid_manual_d100_reuses_normal_check_grading_and_records_provenance():
    services = _services()
    router = CommandRouter(services)
    ctx = AgentCtx(chat_key="cli:dm:manual-valid", user_id="u1", locale="en")
    await _dh2_character(services, ctx)
    request = await _manual_request(router, ctx)

    reply = await router.dispatch_reply(
        ctx, f".__roll_submit {request['request_id']} 17"
    )

    assert reply is not None and reply.error is False
    dice = next(event for event in reply.events if event.kind == "dice")
    assert dice.data["kind"] == "check"
    assert dice.data["rolls"] == [17]
    assert dice.data["total"] == 17
    assert dice.data["target"] == 42
    assert dice.data["effective_target"] == 22
    assert dice.data["detail"]["source"] == "manual"
    assert dice.data["outcome"]["success"] is True

    cancel = next(event for event in reply.events if event.kind == "panel")
    assert cancel.private is True
    assert cancel.data == {"type": "roll_cancel", "request_id": request["request_id"]}
    assert await load_pending_roll(services.store, ctx.chat_key, ctx.user_id) is None

    report = await services.battles.get_current_session(ctx.chat_key)
    assert report is not None
    last_check = report.skill_checks[-1]
    assert last_check.details["source"] == "manual"


async def test_manual_submit_fails_closed_if_the_check_context_changed_while_waiting():
    services = _services()
    router = CommandRouter(services)
    ctx = AgentCtx(chat_key="cli:dm:manual-stale", user_id="u1", locale="en")
    sheet = await _dh2_character(services, ctx)
    request = await _manual_request(router, ctx)

    sheet.attributes["Per"] = 50
    await services.characters.save_character(ctx.user_id, ctx.chat_key, sheet)

    reply = await router.dispatch_reply(
        ctx, f".__roll_submit {request['request_id']} 17"
    )

    assert reply is not None and reply.error is True
    assert not any(event.kind == "dice" for event in reply.events)
    cancel = next(event for event in reply.events if event.kind == "panel")
    assert cancel.data["type"] == "roll_cancel"
    assert await load_pending_roll(services.store, ctx.chat_key, ctx.user_id) is None


async def test_switching_back_to_auto_cancels_a_pending_manual_request():
    services = _services()
    router = CommandRouter(services)
    ctx = AgentCtx(chat_key="cli:dm:manual-cancel", user_id="u1", locale="en")
    await _dh2_character(services, ctx)
    request = await _manual_request(router, ctx)

    reply = await router.dispatch_reply(ctx, ".rollmode auto")

    assert reply is not None and reply.error is False
    cancel = next(event for event in reply.events if event.kind == "panel")
    assert cancel.data == {"type": "roll_cancel", "request_id": request["request_id"]}
    assert await load_pending_roll(services.store, ctx.chat_key, ctx.user_id) is None
