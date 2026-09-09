"""Read-only, system-neutral lifecycle projection for the active character."""

from typing import Any

from core.character_readiness import character_readiness
from core.creation_finalization import (
    creation_finalization_status,
    finalization_row,
    load_creation_finalization_spec,
)


def _localized(values, locale, fallback):
    language = locale.replace("_", "-").casefold()
    return values.get(language) or values.get(language.split("-")[0]) or values.get("en") or fallback


def finalization_surface(pack: Any, character: Any, locale: str) -> dict[str, Any]:
    """Expose only the rolled row, never future/random-table outcomes or effects.

    A malformed managed sheet fails closed. Reading never rolls, applies effects,
    initializes creation, or upgrades a legacy sheet to managed creation.
    """
    readiness = character_readiness(pack, character)
    result = {
        "readiness": {
            "ready": readiness.ready,
            "managed": readiness.managed,
            "phase": readiness.phase,
            "blocked_reference": readiness.blocked_reference,
        }
    }
    spec = load_creation_finalization_spec(pack)
    if spec is None or not readiness.managed:
        return result
    status = creation_finalization_status(pack, character)
    wire: dict[str, Any] = {
        "can_roll": readiness.phase == "finalization" and status is None,
        "can_resolve": readiness.phase == "finalization" and status is not None,
        "complete": bool(status and status.complete),
        "expression": spec.roll,
        "choices": [],
    }
    if status is not None:
        row = finalization_row(spec, status.row_id)
        wire["result"] = {
            "roll": status.roll,
            "id": row.id,
            "label": _localized(row.display, locale, row.id),
            "source": row.source,
            "rules": list(_localized(row.rules, locale, ())),
        }
        if wire["can_resolve"]:
            wire["choices"] = [
                {
                    "id": str(group_id),
                    "label": _localized(group.get("display", {}), locale, str(group_id)),
                    "free": "field_template" in group,
                    "options": [
                        {"id": str(option_id), "label": _localized(option.get("display", {}), locale, str(option_id))}
                        for option_id, option in group.get("options", {}).items()
                    ],
                }
                for group_id, group in row.choices.items()
            ]
    result["finalization"] = wire
    return result
