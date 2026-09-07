import pytest

from core.character_context import (
    CharacterContextError,
    character_context_surface,
    character_context_values,
    load_character_context_spec,
    set_character_context,
)
from core.character_manager import CharacterSheet
from core.rulepacks import load_rulepack


def test_dh2_context_is_optional_and_does_not_assume_inquisition_service():
    pack = load_rulepack("dh2")
    spec = load_character_context_spec(pack)

    assert spec is not None
    assert spec["optional"] is True
    status = spec["fields"]["status"]
    assert set(status["options"]) >= {
        "inquisition",
        "imperial_service",
        "independent",
        "criminal",
        "deserter",
        "renegade",
        "chaos",
        "other",
    }


def test_context_persists_deserter_criminal_premise_without_touching_mechanics():
    pack = load_rulepack("dh2")
    character = CharacterSheet("Mordecai", "dh2")
    before_attributes = dict(character.attributes)
    before_skills = dict(character.skills)

    values = set_character_context(
        pack,
        character,
        {
            "status": "deserter",
            "status_detail": "Бывший комиссар, строящий криминальную империю",
            "allegiance": "disillusioned",
            "goal": "Подчинить себе преступный мир города-улья",
            "campaign_context": "Нашествие Хаоса разрушает привычный порядок",
        },
    )

    assert values["status"] == "deserter"
    assert character_context_values(CharacterSheet.from_dict(character.to_dict())) == values
    assert character.attributes == before_attributes
    assert character.skills == before_skills

    surface = character_context_surface(pack, character, "ru")
    assert surface is not None
    assert surface["complete"] is True
    assert surface["skipped"] is False
    assert surface["values"]["allegiance"] == "disillusioned"
    assert next(field for field in surface["fields"] if field["id"] == "status")["label"] == "Текущее положение"


def test_optional_context_can_be_skipped_and_required_status_is_enforced_when_submitted():
    pack = load_rulepack("dh2")
    character = CharacterSheet("Acolyte", "dh2")

    with pytest.raises(CharacterContextError, match="required"):
        set_character_context(pack, character, {"goal": "Survive"})

    assert set_character_context(pack, character, skip=True) == {}
    surface = character_context_surface(pack, character, "en")
    assert surface is not None
    assert surface["complete"] is True
    assert surface["skipped"] is True
