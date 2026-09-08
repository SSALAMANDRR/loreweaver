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
    assert creation["presentation"]["title"] == "Характеристики и родной мир"
    profiles = {entry["id"]: entry for entry in creation["profiles"]}
    assert profiles["hive_world"]["label"] == "Мир-улей"
    assert any("Ловкость и Восприятие" in line for line in profiles["hive_world"]["detail"])
    assert any("Раны: 8+1к5" in line for line in profiles["hive_world"]["detail"])
    assert profiles["hive_world"]["source"] == "DH2 RU v1.8 p. 42"
    assert "Восприятие" in profiles["hive_world"]["effect"]["grants"]
    forge_choices = {entry["id"]: entry for entry in profiles["forge_world"]["choices"]}
    assert {entry["label"] for entry in forge_choices["home_world_talent"]["options"]} == {
        "Искусный Стук",
        "Длань Омниссии",
    }


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
    assert stage["presentation"]["title"] == "Переброс характеристики"
    assert "окончательным" in stage["presentation"]["effect"]
    assert stage["can_skip"] is True
    assert any(target["id"] == "WS" and isinstance(target["value"], int) for target in stage["targets"])

    context = creation["context"]
    assert context["available"] is True
    assert context["optional"] is True
    assert context["complete"] is False
    fields = {entry["id"]: entry for entry in context["fields"]}
    assert fields["status"]["label"] == "Текущее положение"
    statuses = {entry["id"]: entry["label"] for entry in fields["status"]["options"]}
    assert statuses["inquisition"] == "Служитель Инквизиции"
    assert statuses["deserter"] == "Дезертир / беглец"


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
    assert stage["presentation"]["title"] == "Предыстория"
    options = {entry["id"]: entry for entry in stage["options"]}
    arbites = options["adeptus_arbites"]
    assert arbites["label"] == "Адептус Арбитрес"
    assert "Воплощение Закона" not in arbites.get("detail", [])
    assert any("перебросить" in text.casefold() for text in arbites["detail"])
    choices = {entry["id"]: entry for entry in arbites["choices"]}
    assert choices["trained_skill"]["label"] == "Обученное умение"
    assert {entry["id"] for entry in choices["trained_skill"]["options"]} == {
        "inquiry",
        "interrogation",
    }


def _choice(stage: dict, option_id: str, group_id: str, choice_id: str) -> dict:
    option = next(entry for entry in stage["options"] if entry["id"] == option_id)
    group = next(entry for entry in option["choices"] if entry["id"] == group_id)
    return next(entry for entry in group["options"] if entry["id"] == choice_id)


async def test_state_exposes_open_choice_effects_and_natural_russian_terms():
    services = _services()
    router = CommandRouter(services)
    ctx = AgentCtx(chat_key="cli:dm:state-create-effects", user_id="u1", locale="ru")
    await router.dispatch(ctx, ".dh2 hive_world | Тест")
    await router.dispatch(ctx, ".create done")

    stage = (await build_room_state(services, ctx))["creation"]["stage"]
    medicae = _choice(stage, "adeptus_administratum", "trained_skill", "medicae")

    assert medicae["label"] == "Медицина"
    assert medicae["effect"]["skills"] == [{"label": "Медицина", "value": 1}]

    administratum = next(entry for entry in stage["options"] if entry["id"] == "adeptus_administratum")
    assert "Мастер Бумажной Работы" in administratum["effect"]["grants"]
    assert "медпакет" in administratum["effect"]["equipment"]
