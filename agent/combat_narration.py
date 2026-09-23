"""One scoped prose pass over an already committed deterministic action."""

from __future__ import annotations

import json
from typing import Any

from agent.services import Services
from infra.i18n import get_i18n
from infra.model_call_trace import lane_scope


def _messages(outcome: dict[str, Any], locale: str) -> list[dict[str, str]]:
    """This lane sees the result only; it has no tools or writable game state."""
    return [
        {
            "role": "system",
            "content": get_i18n(locale).t("combat.narration.prompt"),
        },
        {"role": "user", "content": json.dumps(outcome, ensure_ascii=False)},
    ]


async def narrate_combat(services: Services, chat_key: str, outcome: dict[str, Any], locale: str) -> str:
    with lane_scope("combat_narration", chat_key=chat_key):
        response = await services.llm.chat(_messages(outcome, locale), tools=None)
    return (response.content or "").strip()
