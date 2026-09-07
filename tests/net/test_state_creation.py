from agent.context import AgentCtx
from agent.services import build_services
from gateway.commands import CommandRouter
from infra.config import Settings
from infra.embeddings import FakeEmbeddings
from infra.llm import FakeLLM
from net.state import build_room_state


def _services():
    return build_services(Settings(), llm=FakeLLM(script=[]), embeddings=FakeEmbeddings(64))


async def test_state_advertises_profiled_creation_without_client_rule_knowledge():
    services = _services()
    ctx = AgentCtx(chat_key="cli:dm:state-create-catalog", user_id="u1", locale="ru")

    state = await build_room_state(services, ctx)

    dh2 = next(entry for entry in state["systems"] if entry["id"] == "dh2")
    assert dh2["make_char"] == "dh2"
    creation = dh2["creation"]
    assert creation["staged"] is True
    assert creation["requires_profile"] is True
    profiles = {entry["id"]: entry["label"] for entry in creation["profiles"]}
    assert profiles["hive_world"] == "Мир-улей"


async def test_state_restores_current_creation_stage_from_the_saved_character():
    services = _services()
    router = CommandRouter(services)
    ctx = AgentCtx(chat_key="cli:dm:state-create-stage", user_id="u1", locale="ru")
    await router.dispatch(ctx, ".dh2 hive_world | Тест")

    state = await build_room_state(services, ctx)

    creation = state["creation"]
    assert creation["active"] is True
    assert creation["complete"] is False
    assert creation["profile_id"] == "hive_world"
    assert creation["stage_index"] == 1
    assert creation["stage_count"] >= 2
    stage = creation["stage"]
    assert stage["id"] == "characteristic_reroll"
    assert stage["kind"] == "profile_reroll"
    assert stage["can_skip"] is True
    assert any(target["id"] == "WS" and isinstance(target["value"], int) for target in stage["targets"])


async def test_state_projects_layer_options_choices_and_authored_rule_detail():
    services = _services()
    router = CommandRouter(services)
    ctx = AgentCtx(chat_key="cli:dm:state-create-layer", user_id="u1", locale="ru")
    await router.dispatch(ctx, ".dh2 hive_world | Тест")
    await router.dispatch(ctx, ".create done")

    state = await build_room_state(services, ctx)

    stage = state["creation"]["stage"]
    assert stage["id"] == "background"
    assert stage["kind"] == "layer"
    options = {entry["id"]: entry for entry in stage["options"]}
    arbites = options["adeptus_arbites"]
    assert arbites["label"] == "Адептус Арбитрес"
    assert "Воплощение Закона" not in arbites.get("detail", [])
    assert any("перебросить" in text.casefold() for text in arbites["detail"])
    choices = {entry["id"]: entry for entry in arbites["choices"]}
    assert {entry["id"] for entry in choices["trained_skill"]["options"]} == {
        "inquiry",
        "interrogation",
    }
