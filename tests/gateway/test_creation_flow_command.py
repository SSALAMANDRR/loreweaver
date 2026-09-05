from agent.context import AgentCtx
from agent.services import build_services
from core.advancement_surface import initialized_advancement_budget
from core.creation_flow import creation_flow_status
from core.rulepacks import load_rulepack
from core.starting_equipment import starting_equipment_budget
from gateway.commands import CommandRouter
from infra.config import Settings
from infra.embeddings import FakeEmbeddings
from infra.llm import FakeLLM


def _services():
    return build_services(Settings(), llm=FakeLLM(script=[]), embeddings=FakeEmbeddings(64))


async def test_dh2_make_char_starts_flow_and_auto_applies_choice_free_bound_home_world():
    services = _services()
    router = CommandRouter(services)
    ctx = AgentCtx(chat_key="cli:dm:create-start", user_id="u1", locale="en")

    reply = await router.dispatch(ctx, ".dh2 Дикий мир | Acolyte")

    assert reply is not None
    assert "Staged character creation started" in reply
    assert "stage: background" in reply
    assert ".create <option>" in reply

    saved = await services.characters.get_character(ctx.user_id, ctx.chat_key)
    pack = load_rulepack("dh2")
    status = creation_flow_status(pack, saved)
    assert status is not None and status.stage_id == "background"
    assert "Старые Пути" in saved.background_abilities
    assert initialized_advancement_budget(pack, saved) is None
    assert starting_equipment_budget(pack, saved) is None


async def test_forge_world_stays_on_bound_layer_until_required_talent_is_selected():
    services = _services()
    router = CommandRouter(services)
    ctx = AgentCtx(chat_key="cli:dm:create-forge", user_id="u1", locale="en")

    started = await router.dispatch(ctx, ".dh2 Мир-кузница | Tech")

    assert started is not None
    assert "stage: home_world" in started
    assert "home_world_talent" in started
    assert ".create home_world_talent=<value>" in started

    applied = await router.dispatch(ctx, ".create home_world_talent=Искусный Стук")

    assert applied is not None
    assert "stage: background" in applied
    saved = await services.characters.get_character(ctx.user_id, ctx.chat_key)
    assert "Искусный Стук" in saved.talents


async def test_create_previews_layer_choices_without_mutating_character():
    services = _services()
    router = CommandRouter(services)
    ctx = AgentCtx(chat_key="cli:dm:create-preview", user_id="u1", locale="en")
    await router.dispatch(ctx, ".dh2 Дикий мир | Acolyte")
    before = (await services.characters.get_character(ctx.user_id, ctx.chat_key)).to_dict()

    reply = await router.dispatch(ctx, ".create Адептус Администратум")

    assert reply is not None
    assert "trained_skill" in reply
    assert "scholastic_lore" in reply
    assert "weapon_training" in reply
    assert ".create <option> |" in reply
    after = (await services.characters.get_character(ctx.user_id, ctx.chat_key)).to_dict()
    assert after == before


async def test_create_reaches_duplicate_stage_before_initializing_xp():
    services = _services()
    router = CommandRouter(services)
    ctx = AgentCtx(chat_key="cli:dm:create-layers", user_id="u1", locale="en")
    await router.dispatch(ctx, ".dh2 Дикий мир | Acolyte")

    background = await router.dispatch(
        ctx,
        ".create Адептус Администратум"
        " | trained_skill=Коммерция"
        " | scholastic_lore=Бюрократия"
        " | weapon_training=Лазерное"
        " | starting_weapon=Лазпистолет"
        " | aptitude=Познание",
    )
    assert background is not None and "stage: role" in background

    role = await router.dispatch(ctx, ".create Хирургеон | role_talent=Нокдаун")
    assert role is not None and "stage: duplicate_aptitudes" in role
    assert "Aptitudes" in role

    saved = await services.characters.get_character(ctx.user_id, ctx.chat_key)
    pack = load_rulepack("dh2")
    assert initialized_advancement_budget(pack, saved) is None

    duplicates = await router.dispatch(
        ctx,
        ".create Aptitudes=Навык Стрельбы; Навык Рукопашной",
    )
    assert duplicates is not None and "stage: advancement" in duplicates

    saved = await services.characters.get_character(ctx.user_id, ctx.chat_key)
    budget = initialized_advancement_budget(pack, saved)
    assert budget is not None
    assert budget.starting_xp == 1000
    assert budget.available_xp == 1000
    assert len(saved.aptitudes) == len(set(saved.aptitudes))


async def test_create_done_enters_starting_equipment_and_item_purchase_persists():
    services = _services()
    router = CommandRouter(services)
    ctx = AgentCtx(chat_key="cli:dm:create-equipment", user_id="u1", locale="en")
    await router.dispatch(ctx, ".dh2 Дикий мир | Acolyte")
    await router.dispatch(
        ctx,
        ".create Адептус Администратум"
        " | trained_skill=Коммерция"
        " | scholastic_lore=Бюрократия"
        " | weapon_training=Лазерное"
        " | starting_weapon=Лазпистолет"
        " | aptitude=Познание",
    )
    await router.dispatch(ctx, ".create Хирургеон | role_talent=Нокдаун")
    await router.dispatch(ctx, ".create Aptitudes=Навык Стрельбы; Навык Рукопашной")

    equipment_stage = await router.dispatch(ctx, ".create done")

    assert equipment_stage is not None and "stage: starting_equipment" in equipment_stage
    saved = await services.characters.get_character(ctx.user_id, ctx.chat_key)
    pack = load_rulepack("dh2")
    before = starting_equipment_budget(pack, saved)
    assert before is not None and before.remaining >= 1

    bought = await router.dispatch(ctx, ".create Лазган")

    assert bought is not None and "Added Лазган" in bought
    saved = await services.characters.get_character(ctx.user_id, ctx.chat_key)
    after = starting_equipment_budget(pack, saved)
    assert after is not None and after.remaining == before.remaining - 1
    assert "Лазган" in saved.equipment
    assert any("2 магазина" in item for item in saved.equipment)
