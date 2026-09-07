from agent.context import AgentCtx
from agent.services import build_services
from gateway.commands import CommandRouter
from infra.config import Settings
from infra.embeddings import FakeEmbeddings
from infra.llm import FakeLLM


def _services():
    return build_services(Settings(), llm=FakeLLM(script=[]), embeddings=FakeEmbeddings(64))


def _panel(reply, frame_type: str):
    assert reply is not None
    return next(
        event.data
        for event in reply.events
        if event.kind == "panel" and event.data.get("type") == frame_type
    )


async def test_creation_catalog_is_private_structured_pack_data():
    services = _services()
    router = CommandRouter(services)
    ctx = AgentCtx(chat_key="cli:dm:creation-ui-catalog", user_id="u1", locale="ru")

    reply = await router.dispatch_reply(ctx, ".__creation_catalog dh2")

    assert reply is not None and reply.error is True and reply.text == ""
    event = next(event for event in reply.events if event.kind == "panel")
    assert event.private is True
    frame = event.data
    assert frame["type"] == "creation_catalog"
    assert frame["system"] == "dh2"
    assert frame["requires_profile"] is True
    assert frame["staged"] is True
    profiles = {entry["id"]: entry["label"] for entry in frame["profiles"]}
    assert "hive_world" in profiles
    assert profiles["hive_world"] == "Мир-улей"


async def test_creation_state_exposes_reroll_targets_without_parsing_command_text():
    services = _services()
    router = CommandRouter(services)
    ctx = AgentCtx(chat_key="cli:dm:creation-ui-reroll", user_id="u1", locale="ru")
    await router.dispatch(ctx, ".dh2 hive_world | Тест")

    reply = await router.dispatch_reply(ctx, ".__creation_state")

    frame = _panel(reply, "creation_state")
    assert frame["active"] is True
    assert frame["complete"] is False
    assert frame["profile_id"] == "hive_world"
    assert frame["stage"]["id"] == "characteristic_reroll"
    assert frame["stage"]["kind"] == "profile_reroll"
    assert frame["stage"]["can_skip"] is True
    targets = {entry["id"]: entry for entry in frame["stage"]["targets"]}
    assert "WS" in targets
    assert isinstance(targets["WS"]["value"], int)


async def test_creation_state_exposes_layer_options_and_required_choice_groups():
    services = _services()
    router = CommandRouter(services)
    ctx = AgentCtx(chat_key="cli:dm:creation-ui-layer", user_id="u1", locale="ru")
    await router.dispatch(ctx, ".dh2 hive_world | Тест")
    await router.dispatch(ctx, ".create done")

    reply = await router.dispatch_reply(ctx, ".__creation_state")

    frame = _panel(reply, "creation_state")
    stage = frame["stage"]
    assert stage["id"] == "background"
    assert stage["kind"] == "layer"
    assert stage["fixed"] is False
    options = {entry["id"]: entry for entry in stage["options"]}
    arbites = options["adeptus_arbites"]
    assert arbites["label"] == "Адептус Арбитрес"
    choices = {entry["id"]: entry for entry in arbites["choices"]}
    assert "trained_skill" in choices
    assert {entry["id"] for entry in choices["trained_skill"]["options"]} == {
        "inquiry",
        "interrogation",
    }


async def test_creation_state_without_character_is_an_inactive_private_frame():
    services = _services()
    router = CommandRouter(services)
    ctx = AgentCtx(chat_key="cli:dm:creation-ui-empty", user_id="u1", locale="en")

    reply = await router.dispatch_reply(ctx, ".__creation_state")

    frame = _panel(reply, "creation_state")
    assert frame["active"] is False
    assert frame["stage"] is None
