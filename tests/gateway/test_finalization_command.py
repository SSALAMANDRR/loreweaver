from types import SimpleNamespace

from agent.context import AgentCtx
from agent.services import build_services
from core.character_manager import CharacterSheet
from core.creation_finalization import creation_finalization_status
from core.creation_flow import load_creation_flow_spec
from core.rulepacks import load_rulepack
from core.sheets import set_sheet_value, sheet_value
from gateway.commands import CommandRouter
from infra.config import Settings
from infra.embeddings import FakeEmbeddings
from infra.llm import FakeLLM


def _services():
    return build_services(Settings(), llm=FakeLLM(script=[]), embeddings=FakeEmbeddings(64))


def _completed_creation_sheet() -> CharacterSheet:
    pack = load_rulepack("dh2")
    flow = load_creation_flow_spec(pack)
    assert flow is not None
    sheet = CharacterSheet("Acolyte", "dh2")
    for canonical in ("WS", "BS", "S", "T", "Ag", "Int", "Per", "WP", "Fel", "Inf"):
        set_sheet_value(sheet, pack, canonical, 30)
    set_sheet_value(sheet, pack, "FateThreshold", 2)
    set_sheet_value(sheet, pack, "Fate", 2)
    sheet.secondary_attributes["__creation_flow__"] = {
        "version": 1,
        "profile_id": "feral_world",
        "profile_reroll": {"skipped": True},
        "stage_index": len(flow.stages),
        "completed": [stage.id for stage in flow.stages],
        "layers": {},
    }
    return sheet


async def test_finalize_prompts_for_the_mandatory_roll_after_staged_creation():
    services = _services()
    router = CommandRouter(services)
    ctx = AgentCtx(chat_key="cli:dm:finalize-prompt", user_id="u1", locale="en")
    await services.characters.save_character(ctx.user_id, ctx.chat_key, _completed_creation_sheet())

    reply = await router.dispatch(ctx, ".finalize")

    assert reply is not None
    assert ".finalize roll" in reply
    saved = await services.characters.get_character(ctx.user_id, ctx.chat_key)
    assert creation_finalization_status(load_rulepack("dh2"), saved) is None


async def test_finalize_roll_applies_choice_free_divination_and_marks_character_ready(monkeypatch):
    services = _services()
    router = CommandRouter(services)
    ctx = AgentCtx(chat_key="cli:dm:finalize-ready", user_id="u1", locale="en")
    await services.characters.save_character(ctx.user_id, ctx.chat_key, _completed_creation_sheet())
    monkeypatch.setattr(services.dice, "roll_expression", lambda expression: SimpleNamespace(total=100))

    reply = await router.dispatch(ctx, ".finalize roll")

    assert reply is not None
    assert "Finalization roll 100" in reply
    assert "ready for play" in reply
    saved = await services.characters.get_character(ctx.user_id, ctx.chat_key)
    pack = load_rulepack("dh2")
    status = creation_finalization_status(pack, saved)
    assert status is not None and status.complete is True
    assert sheet_value(saved, pack, "FateThreshold") == 3
    assert sheet_value(saved, pack, "Fate") == 3


async def test_finalize_choice_result_is_rolled_once_then_resolved_explicitly(monkeypatch):
    services = _services()
    router = CommandRouter(services)
    ctx = AgentCtx(chat_key="cli:dm:finalize-choice", user_id="u1", locale="en")
    await services.characters.save_character(ctx.user_id, ctx.chat_key, _completed_creation_sheet())
    monkeypatch.setattr(services.dice, "roll_expression", lambda expression: SimpleNamespace(total=18))

    rolled = await router.dispatch(ctx, ".finalize roll")

    assert rolled is not None
    assert "Finalization roll 18" in rolled
    assert "increase" in rolled and "decrease" in rolled
    assert ".finalize increase=<value> | decrease=<value>" in rolled

    resolved = await router.dispatch(
        ctx,
        ".finalize increase=Ловкость | decrease=Навык Стрельбы",
    )

    assert resolved is not None and "ready for play" in resolved
    saved = await services.characters.get_character(ctx.user_id, ctx.chat_key)
    pack = load_rulepack("dh2")
    assert sheet_value(saved, pack, "Ag") == 33
    assert sheet_value(saved, pack, "BS") == 27
    status = creation_finalization_status(pack, saved)
    assert status is not None and status.roll == 18 and status.complete is True


async def test_finalize_roll_one_is_preserved_as_blocked_and_never_rerolled(monkeypatch):
    services = _services()
    router = CommandRouter(services)
    ctx = AgentCtx(chat_key="cli:dm:finalize-blocked", user_id="u1", locale="en")
    await services.characters.save_character(ctx.user_id, ctx.chat_key, _completed_creation_sheet())
    calls = 0

    def fixed_roll(expression):
        nonlocal calls
        calls += 1
        return SimpleNamespace(total=1 if calls == 1 else 100)

    monkeypatch.setattr(services.dice, "roll_expression", fixed_roll)

    first = await router.dispatch(ctx, ".finalize roll")
    second = await router.dispatch(ctx, ".finalize roll")

    assert first is not None and "table_8_15_rudiments" in first
    assert second is not None and "table_8_15_rudiments" in second
    assert calls == 1
    saved = await services.characters.get_character(ctx.user_id, ctx.chat_key)
    status = creation_finalization_status(load_rulepack("dh2"), saved)
    assert status is not None and status.roll == 1 and status.complete is False


async def test_finalize_is_rejected_before_staged_creation_finishes():
    services = _services()
    router = CommandRouter(services)
    ctx = AgentCtx(chat_key="cli:dm:finalize-too-early", user_id="u1", locale="en")
    sheet = CharacterSheet("Acolyte", "dh2")
    await services.characters.save_character(ctx.user_id, ctx.chat_key, sheet)

    reply = await router.dispatch(ctx, ".finalize roll")

    assert reply is not None
    assert "Finish the staged character-creation flow" in reply
    saved = await services.characters.get_character(ctx.user_id, ctx.chat_key)
    assert creation_finalization_status(load_rulepack("dh2"), saved) is None
