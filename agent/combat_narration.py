"""One scoped prose pass over an already committed deterministic action.

The lane's whole input is PLAYER-grade: the caller passes the player projection of
the committed result, and the scene/encounter/NPC context below is read through
the same player projections every other public surface uses. Its output is
broadcast to the table, so nothing keeper-only may reach it.
"""

from __future__ import annotations

import json
from typing import Any

from agent.services import Services
from core.documents import PLAYER_VIEWER, SCENE_ID
from infra.i18n import get_i18n
from infra.model_call_trace import lane_scope


async def _scene_context(
    services: Services, chat_key: str, result: dict[str, Any], encounter: dict[str, Any] | None
) -> dict[str, Any]:
    from agent.npc import NPC_DOC_TYPE, list_npcs

    context: dict[str, Any] = {}
    try:
        scene = await services.documents.get_view(chat_key, "scene", SCENE_ID, PLAYER_VIEWER)
        if scene and scene.get("name"):
            context["scene"] = {key: scene[key] for key in ("name", "focus") if scene.get(key)}
    except Exception:
        pass
    if encounter is not None:
        context["encounter"] = {
            "round": encounter.get("round_number"),
            "order": [entry.get("name") for entry in encounter.get("order") or []],
        }
    involved = {str(result.get("actor") or ""), str(result.get("target") or "")} - {""}
    try:
        people = []
        for record in await list_npcs(services.documents, chat_key):
            if (record.stat_char or record.name) not in involved:
                continue
            view = await services.documents.get_view(chat_key, NPC_DOC_TYPE, record.id, PLAYER_VIEWER)
            if view:
                people.append(view)
        if people:
            context["npcs"] = people
    except Exception:
        pass
    return context


def _messages(outcome: dict[str, Any], context: dict[str, Any], locale: str) -> list[dict[str, str]]:
    """This lane sees the result and player-grade context only; it has no tools or writable state."""
    payload = {"result": outcome, **({"context": context} if context else {})}
    return [
        {
            "role": "system",
            "content": get_i18n(locale).t("combat.narration.prompt"),
        },
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


async def narrate_combat(
    services: Services,
    chat_key: str,
    outcome: dict[str, Any],
    locale: str,
    *,
    encounter: dict[str, Any] | None = None,
) -> str:
    """`outcome` and `encounter` must already be player-grade projections."""
    context = await _scene_context(services, chat_key, outcome, encounter)
    with lane_scope("combat_narration", chat_key=chat_key):
        response = await services.llm.chat(_messages(outcome, context, locale), tools=None)
    return (response.content or "").strip()
