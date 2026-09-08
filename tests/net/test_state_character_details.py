from agent.context import AgentCtx
from agent.services import build_services
from core.character_manager import CharacterSheet
from infra.config import Settings
from infra.embeddings import FakeEmbeddings
from infra.llm import FakeLLM
from net.state import build_room_state


def _services():
    return build_services(Settings(), llm=FakeLLM(script=[]), embeddings=FakeEmbeddings(64))


async def test_state_projects_skills_talents_equipment_and_hover_help_for_rich_clients():
    services = _services()
    ctx = AgentCtx(chat_key="cli:dm:state-sheet-details", user_id="u1", locale="ru")
    sheet = CharacterSheet("Тест", "dh2")
    sheet.skills["Medicae"] = 2
    sheet.skills["Navigation::Варп"] = 1
    sheet.talents = ["Вскочить", "Выучка с Оружием (Лазерное)"]
    sheet.equipment = ["Лазган", "Стандартные боеприпасы: Лазган (2 магазина)"]
    await services.characters.save_character(ctx.user_id, ctx.chat_key, sheet)

    state = await build_room_state(services, ctx)
    character = state["character"]

    assert "ближнем бою" in character["attribute_help"]["WS"]
    assert character["skills"]["Medicae"] == 2
    assert character["skill_labels"]["Medicae"] == "Медицина"
    assert character["skill_labels"]["Navigation::Варп"] == "Навигация (Варп)"
    assert "первая помощь" in character["skill_help"]["Medicae"].casefold()
    assert "прокладывать путь" in character["skill_help"]["Navigation::Варп"].casefold()
    assert character["talents"] == ["Вскочить", "Выучка с Оружием (Лазерное)"]
    assert character["equipment"] == [
        "Лазган",
        "Стандартные боеприпасы: Лазган (2 магазина)",
    ]
    lasgun = character["equipment_details"]["Лазган"]
    assert lasgun["kind"] == "weapon"
    assert lasgun["availability"] == 10
    assert lasgun["source"] == "CH05_H076"
