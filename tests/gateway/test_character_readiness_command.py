from agent.context import AgentCtx
from agent.services import build_services
from core.character_manager import CharacterSheet
from core.creation_flow import load_creation_flow_spec
from core.rulepacks import load_rulepack
from core.sheets import set_sheet_value
from gateway.commands import CommandRouter
from infra.config import Settings
from infra.embeddings import FakeEmbeddings
from infra.llm import FakeLLM


def _services():
    return build_services(Settings(), llm=FakeLLM(script=[]), embeddings=FakeEmbeddings(64))


def _managed_sheet(*, complete_flow: bool, finalized: bool = False, blocked: bool = False) -> CharacterSheet:
    pack = load_rulepack("dh2")
    flow = load_creation_flow_spec(pack)
    assert flow is not None
    sheet = CharacterSheet("Acolyte", "dh2")
    for canonical in ("WS", "BS", "S", "T", "Ag", "Int", "Per", "WP", "Fel", "Inf"):
        set_sheet_value(sheet, pack, canonical, 30)
    sheet.secondary_attributes["__creation_flow__"] = {
        "version": 1,
        "profile_id": "feral_world",
        "profile_reroll": {"skipped": True} if complete_flow else None,
        "stage_index": len(flow.stages) if complete_flow else 1,
        "completed": [stage.id for stage in flow.stages] if complete_flow else [flow.stages[0].id],
        "layers": {},
    }
    if blocked:
        sheet.secondary_attributes["__creation_finalization__"] = {
            "version": 1,
            "roll": 1,
            "row_id": "mutation_without_corruption",
            "selections": {},
            "complete": False,
            "blocked_reference": "table_8_15_rudiments",
        }
    elif finalized:
        sheet.secondary_attributes["__creation_finalization__"] = {
            "version": 1,
            "roll": 100,
            "row_id": "ask_how_you_serve",
            "selections": {},
            "complete": True,
            "blocked_reference": "",
        }
    return sheet


async def test_character_check_is_blocked_during_managed_creation():
    services = _services()
    router = CommandRouter(services)
    ctx = AgentCtx(chat_key="cli:dm:readiness-create", user_id="u1", locale="en")
    await services.characters.save_character(ctx.user_id, ctx.chat_key, _managed_sheet(complete_flow=False))

    reply = await router.dispatch(ctx, ".check Awareness")

    assert reply is not None
    assert "Finish staged character creation" in reply
    assert ".create" in reply


async def test_completed_flow_is_blocked_until_finalization_finishes():
    services = _services()
    router = CommandRouter(services)
    ctx = AgentCtx(chat_key="cli:dm:readiness-finalize", user_id="u1", locale="en")
    await services.characters.save_character(ctx.user_id, ctx.chat_key, _managed_sheet(complete_flow=True))

    reply = await router.dispatch(ctx, ".check Awareness")

    assert reply is not None
    assert "required finalization" in reply
    assert ".finalize" in reply


async def test_blocked_finalization_exposes_reference_without_allowing_character_play():
    services = _services()
    router = CommandRouter(services)
    ctx = AgentCtx(chat_key="cli:dm:readiness-blocked", user_id="u1", locale="en")
    await services.characters.save_character(
        ctx.user_id,
        ctx.chat_key,
        _managed_sheet(complete_flow=True, blocked=True),
    )

    reply = await router.dispatch(ctx, ".init roll")

    assert reply is not None
    assert "table_8_15_rudiments" in reply
    assert "do not reroll" in reply


async def test_invalid_managed_state_fails_closed_instead_of_allowing_character_play():
    services = _services()
    router = CommandRouter(services)
    ctx = AgentCtx(chat_key="cli:dm:readiness-invalid", user_id="u1", locale="en")
    sheet = _managed_sheet(complete_flow=False)
    sheet.secondary_attributes["__creation_flow__"]["stage_index"] = "broken"
    await services.characters.save_character(ctx.user_id, ctx.chat_key, sheet)

    reply = await router.dispatch(ctx, ".check Awareness")

    assert reply is not None
    assert "managed creation state is invalid" in reply
    assert "blocked" in reply


async def test_finalized_managed_character_can_use_character_checks():
    services = _services()
    router = CommandRouter(services)
    ctx = AgentCtx(chat_key="cli:dm:readiness-ready", user_id="u1", locale="en")
    await services.characters.save_character(
        ctx.user_id,
        ctx.chat_key,
        _managed_sheet(complete_flow=True, finalized=True),
    )

    reply = await router.dispatch(ctx, ".check Awareness")

    assert reply is not None
    assert "Finish staged character creation" not in reply
    assert "required finalization" not in reply
    assert "Check" in reply


async def test_initiative_tracker_view_remains_available_during_creation():
    services = _services()
    router = CommandRouter(services)
    ctx = AgentCtx(chat_key="cli:dm:readiness-init-list", user_id="u1", locale="en")
    await services.characters.save_character(ctx.user_id, ctx.chat_key, _managed_sheet(complete_flow=False))

    reply = await router.dispatch(ctx, ".init")

    assert reply is not None
    assert "Finish staged character creation" not in reply
