from agent.context import AgentCtx
from agent.services import build_services
from core.advancement_purchase import advancement_budget, initialize_advancement_budget
from core.character_manager import CharacterSheet
from core.rulepacks import load_rulepack
from gateway.commands import CommandRouter
from infra.config import Settings
from infra.embeddings import FakeEmbeddings
from infra.llm import FakeLLM


def _services():
    return build_services(Settings(), llm=FakeLLM(script=[]), embeddings=FakeEmbeddings(64))


async def _save_dh2(services, ctx, *, aptitudes=()):
    character = CharacterSheet("Acolyte", "dh2")
    character.aptitudes = list(aptitudes)
    character.attributes["Ag"] = 30
    character.attributes["Int"] = 30
    initialize_advancement_budget(load_rulepack("dh2"), character)
    await services.characters.save_character(ctx.user_id, ctx.chat_key, character)
    return character


async def test_advance_lists_budget_and_next_purchases():
    services = _services()
    router = CommandRouter(services)
    ctx = AgentCtx(chat_key="cli:dm:advance-list", user_id="u1", locale="en")
    await _save_dh2(services, ctx, aptitudes=["Ловкость", "Изящество"])

    reply = await router.dispatch(ctx, ".advance")

    assert reply is not None
    assert "XP: 1000 available" in reply
    assert "Ag" in reply
    assert "100 XP" in reply
    assert "Быстрая Перезарядка" in reply


async def test_advance_purchases_characteristic_and_persists_budget():
    services = _services()
    router = CommandRouter(services)
    ctx = AgentCtx(chat_key="cli:dm:advance-buy", user_id="u1", locale="en")
    await _save_dh2(services, ctx, aptitudes=["Ловкость", "Изящество"])

    reply = await router.dispatch(ctx, ".advance characteristic Ag")

    assert reply is not None and "900 XP remain" in reply
    saved = await services.characters.get_character(ctx.user_id, ctx.chat_key)
    assert saved.attributes["Ag"] == 35
    budget = advancement_budget(load_rulepack("dh2"), saved)
    assert budget is not None
    assert (budget.available_xp, budget.spent_xp) == (900, 100)


async def test_xp_alias_purchases_explicit_specialized_skill():
    services = _services()
    router = CommandRouter(services)
    ctx = AgentCtx(chat_key="cli:dm:advance-special", user_id="u1", locale="en")
    await _save_dh2(services, ctx, aptitudes=["Интеллект", "Полевое"])

    reply = await router.dispatch(ctx, ".xp skill Навигация (варп)")

    assert reply is not None and "900 XP remain" in reply
    saved = await services.characters.get_character(ctx.user_id, ctx.chat_key)
    assert saved.skills["Navigation::варп"] == 1


async def test_advance_rejects_bare_special_skill_family():
    services = _services()
    router = CommandRouter(services)
    ctx = AgentCtx(chat_key="cli:dm:advance-bare-family", user_id="u1", locale="en")
    await _save_dh2(services, ctx, aptitudes=["Интеллект", "Полевое"])

    reply = await router.dispatch(ctx, ".advance skill Навигация")

    assert reply is not None and "cannot be bought" in reply
    saved = await services.characters.get_character(ctx.user_id, ctx.chat_key)
    assert "Navigation" not in saved.skills
    budget = advancement_budget(load_rulepack("dh2"), saved)
    assert budget is not None and budget.available_xp == 1000


async def test_advance_purchases_catalog_talent_and_persists_it():
    services = _services()
    router = CommandRouter(services)
    ctx = AgentCtx(chat_key="cli:dm:advance-talent", user_id="u1", locale="en")
    await _save_dh2(services, ctx, aptitudes=["Ловкость", "Полевое"])

    reply = await router.dispatch(ctx, ".advance talent Быстрая Перезарядка")

    assert reply is not None and "800 XP remain" in reply
    assert "0→1" not in reply
    saved = await services.characters.get_character(ctx.user_id, ctx.chat_key)
    assert "Быстрая Перезарядка" in saved.talents
    budget = advancement_budget(load_rulepack("dh2"), saved)
    assert budget is not None
    assert (budget.available_xp, budget.spent_xp) == (800, 200)


async def test_advance_talent_requires_declared_specialization():
    services = _services()
    router = CommandRouter(services)
    ctx = AgentCtx(chat_key="cli:dm:advance-talent-special", user_id="u1", locale="en")
    await _save_dh2(services, ctx, aptitudes=["Общая", "Изящество"])

    bad = await router.dispatch(ctx, ".advance talent Выучка с Оружием")
    good = await router.dispatch(ctx, ".advance talent Выучка с Оружием (Лазерное)")

    assert bad is not None and "cannot be bought" in bad
    assert good is not None and "800 XP remain" in good
    saved = await services.characters.get_character(ctx.user_id, ctx.chat_key)
    assert saved.talents == ["Выучка с Оружием (Лазерное)"]


async def test_advance_does_not_grant_xp_to_uninitialized_legacy_sheet():
    services = _services()
    router = CommandRouter(services)
    ctx = AgentCtx(chat_key="cli:dm:advance-uninitialized", user_id="u1", locale="en")
    character = CharacterSheet("Legacy", "dh2")
    await services.characters.save_character(ctx.user_id, ctx.chat_key, character)

    reply = await router.dispatch(ctx, ".advance")

    assert reply is not None and "no initialized XP budget" in reply
    saved = await services.characters.get_character(ctx.user_id, ctx.chat_key)
    assert "__advancement__" not in saved.secondary_attributes
