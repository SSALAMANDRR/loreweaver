from agent.context import AgentCtx
from agent.services import build_services
from core.creation_flow import creation_flow_status
from core.rulepacks import load_rulepack
from gateway.commands import CommandRouter
from infra.config import Settings
from infra.embeddings import FakeEmbeddings
from infra.llm import FakeLLM


def _services():
    return build_services(Settings(), llm=FakeLLM(script=[]), embeddings=FakeEmbeddings(64))


async def test_hidden_start_uses_normal_profiled_creation_but_suppresses_success_prose():
    services = _services()
    router = CommandRouter(services)
    ctx = AgentCtx(chat_key="cli:dm:creation-action-start", user_id="u1", locale="ru")

    reply = await router.dispatch_reply(
        ctx,
        ".__creation_action start dh2 | hive_world | Тест",
    )

    assert reply is not None and reply.error is False and reply.text == ""
    saved = await services.characters.get_character(ctx.user_id, ctx.chat_key)
    assert saved.name == "Тест"
    assert saved.system == "dh2"
    status = creation_flow_status(load_rulepack("dh2"), saved)
    assert status is not None and status.stage_id == "characteristic_reroll"


async def test_hidden_create_step_delegates_to_normal_flow_and_keeps_success_silent():
    services = _services()
    router = CommandRouter(services)
    ctx = AgentCtx(chat_key="cli:dm:creation-action-step", user_id="u1", locale="ru")
    await router.dispatch_reply(ctx, ".__creation_action start dh2 | hive_world | Тест")

    reply = await router.dispatch_reply(ctx, ".__creation_action create done")

    assert reply is not None and reply.error is False and reply.text == ""
    saved = await services.characters.get_character(ctx.user_id, ctx.chat_key)
    status = creation_flow_status(load_rulepack("dh2"), saved)
    assert status is not None and status.stage_id == "background"


async def test_hidden_creation_action_preserves_underlying_failures():
    services = _services()
    router = CommandRouter(services)
    ctx = AgentCtx(chat_key="cli:dm:creation-action-error", user_id="u1", locale="ru")

    reply = await router.dispatch_reply(ctx, ".__creation_action start dh2 | no_such_profile | Тест")

    assert reply is not None and reply.error is True
    assert reply.text
