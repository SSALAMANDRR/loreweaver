from agent.context import AgentCtx
from agent.services import build_services
from core.dice_engine import seed_dice
from gateway.commands import CommandRouter
from infra.config import Settings
from infra.embeddings import FakeEmbeddings
from infra.llm import FakeLLM


def _services():
    return build_services(Settings(), llm=FakeLLM(script=[]), embeddings=FakeEmbeddings(64))


async def test_dh2_make_char_requires_a_creation_profile():
    services = _services()
    router = CommandRouter(services)
    ctx = AgentCtx(chat_key="cli:dm:dh2-create-missing", user_id="u1", locale="en")

    reply = await router.dispatch(ctx, ".dh2")

    assert reply is not None
    assert await services.store.state_get(ctx.chat_key, "active_character.u1") is None


async def test_dh2_make_char_accepts_canonical_profile_id_and_character_name():
    services = _services()
    router = CommandRouter(services)
    ctx = AgentCtx(chat_key="cli:dm:dh2-create-id", user_id="u1", locale="en")

    seed_dice(2026)
    reply = await router.dispatch(ctx, ".dh2 forge_world Varro")

    assert reply is not None and "dh2" in reply
    character = await services.characters.get_character("u1", ctx.chat_key)
    assert character.name == "Varro"
    assert character.system == "dh2"
    assert character.home_world == "Мир-кузница"
    assert character.attributes["FATE_THRESHOLD"] in {3, 4}
    assert character.attributes["FATE"] == character.attributes["FATE_THRESHOLD"]
    for key in ("WS", "BS", "S", "T", "Ag", "Int", "Per", "WP", "Fel", "Inf"):
        assert 22 <= character.attributes[key] <= 40


async def test_dh2_make_char_accepts_multiword_profile_alias_with_pipe_separator():
    services = _services()
    router = CommandRouter(services)
    ctx = AgentCtx(chat_key="cli:dm:dh2-create-alias", user_id="u1", locale="en")

    seed_dice(2026)
    reply = await router.dispatch(ctx, ".dh2 Мир-улей | Мордекай")

    assert reply is not None and "dh2" in reply
    character = await services.characters.get_character("u1", ctx.chat_key)
    assert character.name == "Мордекай"
    assert character.home_world == "Мир-улей"
    assert character.attributes["FATE_THRESHOLD"] in {2, 3}
    assert character.attributes["FATE"] == character.attributes["FATE_THRESHOLD"]


async def test_non_profiled_make_char_commands_keep_the_legacy_path():
    services = _services()
    router = CommandRouter(services)
    ctx = AgentCtx(chat_key="cli:dm:coc-create-regression", user_id="u1", locale="en")

    seed_dice(2026)
    reply = await router.dispatch(ctx, ".coc Nora")

    assert reply is not None and "coc7" in reply
    character = await services.characters.get_character("u1", ctx.chat_key)
    assert character.name == "Nora"
    assert character.system == "coc7"
