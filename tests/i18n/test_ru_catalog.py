"""Russian creation messages preserve interpolation and explicit fallback."""

import json
from pathlib import Path
from string import Formatter

from infra.i18n import I18n


def test_russian_catalog_is_discoverable_and_renders_creation():
    i18n = I18n("ru")
    assert "ru" in i18n.available_locales()
    assert i18n.t("commands.creation.header", stage="Родной мир") == "Создание персонажа · этап: Родной мир"
    assert i18n.t("commands.manual_roll.unsupported", expression="2d10") == (
        "Для этого броска пока нельзя использовать физические кости: 2d10"
    )
    assert i18n.t("common.yes") == I18n("en").t("common.yes")


def test_russian_translations_preserve_all_format_fields():
    root = Path(__file__).resolve().parents[2] / "locales"
    formatter = Formatter()

    def fields(text):
        return sorted((field, spec, conversion) for _, field, spec, conversion in formatter.parse(text) if field is not None)

    for path in (root / "ru").glob("*.json"):
        en = json.loads((root / "en" / path.name).read_text(encoding="utf-8"))
        ru = json.loads(path.read_text(encoding="utf-8"))
        for key, value in ru.items():
            assert value.strip(), key
            assert fields(value) == fields(en[key]), key
