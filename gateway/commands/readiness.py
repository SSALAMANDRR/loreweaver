"""Character-readiness gates for character-driven play commands.

The gate is intentionally narrow: free-form dice rolls and character-maintenance
commands remain available during creation, while checks and initiative that use
the active sheet wait until a managed creation lifecycle is complete.
"""

from __future__ import annotations

from core.character_manager import has_character
from core.character_readiness import character_readiness
from core.rulepacks import load_rulepack
from gateway.commands.types import CommandCtx

_TRACKER_ONLY_INIT_ACTIONS = {"", "show", "list", "next", "clear"}


async def _not_ready_notice(ctx: CommandCtx) -> str | None:
    character = await ctx.services.characters.get_character(ctx.user_id, ctx.chat_key)
    if not has_character(character):
        return None
    try:
        pack = load_rulepack(character.system)
        readiness = character_readiness(pack, character)
    except Exception:
        return None
    if readiness.ready:
        return None
    if readiness.phase == "creation":
        return ctx.i18n.t("commands.readiness.creation")
    if readiness.phase == "blocked":
        return ctx.i18n.t(
            "commands.readiness.blocked",
            reference=readiness.blocked_reference,
        )
    return ctx.i18n.t("commands.readiness.finalization")


class ReadinessCommands:
    """Block sheet-driven play while a managed character is still being created."""

    async def cmd_check(self, ctx: CommandCtx) -> str:
        notice = await _not_ready_notice(ctx)
        if notice is not None:
            return ctx.fail(notice)
        return await super().cmd_check(ctx)

    async def cmd_opposed(self, ctx: CommandCtx) -> str:
        notice = await _not_ready_notice(ctx)
        if notice is not None:
            return ctx.fail(notice)
        return await super().cmd_opposed(ctx)

    async def cmd_sanity(self, ctx: CommandCtx) -> str:
        notice = await _not_ready_notice(ctx)
        if notice is not None:
            return ctx.fail(notice)
        return await super().cmd_sanity(ctx)

    async def cmd_growth(self, ctx: CommandCtx) -> str:
        notice = await _not_ready_notice(ctx)
        if notice is not None:
            return ctx.fail(notice)
        return await super().cmd_growth(ctx)

    async def cmd_initiative(self, ctx: CommandCtx) -> str:
        if ctx.args.strip().casefold() not in _TRACKER_ONLY_INIT_ACTIONS:
            notice = await _not_ready_notice(ctx)
            if notice is not None:
                return ctx.fail(notice)
        return await super().cmd_initiative(ctx)
