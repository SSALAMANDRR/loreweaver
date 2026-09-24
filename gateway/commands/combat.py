"""The keeper's `.combat` encounter control: start (server-rolled initiative), end, hide/reveal.

Turn actions themselves travel as `action_request` frames; this module only opens,
closes and curates the encounter those frames act on.
"""

from __future__ import annotations

from agent.context import AgentCtx
from core.combat import CombatValidationError
from gateway.combat_actions import (
    end_room_encounter,
    load_encounter,
    set_combatant_hidden,
    start_room_encounter,
)
from gateway.commands.rooms import _is_keeper
from gateway.commands.types import CommandCtx
from gateway.turn import publish_state

_HIDDEN_MARK = "?"
_ALLY_MARK = "+"


# Engine diagnostics a keeper can act on, by the fragment that identifies them.
_START_FAILURES = (
    ("at least two", "combat.command.start.too_few"),
    ("already active", "combat.command.start.active"),
    ("not keeper-controlled", "combat.command.start.not_npc"),
    ("already defeated", "combat.command.start.defeated"),
    ("unavailable", "combat.command.start.unknown"),
    ("rulepack", "combat.command.start.system"),
)


def _start_failure(ctx: CommandCtx, reason: str) -> str:
    for fragment, key in _START_FAILURES:
        if fragment in reason:
            return ctx.i18n.t(key)
    return ctx.i18n.t("combat.command.start_failed", reason=reason)


class CombatCommands:
    """`CommandRouter` mixin — see the module docstring."""

    def _combat_agent_ctx(self, ctx: CommandCtx) -> AgentCtx:
        raw = ctx.raw_ctx
        return AgentCtx(
            chat_key=ctx.chat_key,
            user_id=ctx.user_id,
            platform=str(getattr(raw, "platform", "cli") or "cli"),
            locale=ctx.locale,
            extra=getattr(raw, "extra", {}) or {},
        )

    async def _combat_order(self, ctx: CommandCtx) -> str:
        state = await load_encounter(ctx.services, ctx.chat_key)
        if state is None:
            return ctx.i18n.t("combat.command.none")
        lines = [ctx.i18n.t("combat.command.status", round=state.round_number, current=state.current_actor)]
        for index, name in enumerate(state.order, 1):
            combatant = state.combatants[name]
            lines.append(
                ctx.i18n.t(
                    "combat.command.order_line",
                    index=index,
                    name=name,
                    initiative=combatant.initiative if combatant.initiative is not None else "-",
                    marker=(
                        (ctx.i18n.t("combat.command.hidden_marker") if combatant.hidden else "")
                        + (ctx.i18n.t("combat.command.defeated_marker") if combatant.defeated else "")
                    ),
                )
            )
        return "\n".join(lines)

    async def cmd_combat(self, ctx: CommandCtx) -> str:
        """`.combat [status | start <npc>[, ?<hidden npc>, +<allied npc>…] | end | hide <name> | reveal <name>]`."""
        if not _is_keeper(ctx.raw_ctx):
            return ctx.fail(ctx.i18n.t("rooms.denied"))
        sub, _, rest = ctx.args.strip().partition(" ")
        sub = sub.casefold()
        rest = rest.strip()
        agent_ctx = self._combat_agent_ctx(ctx)
        if sub in {"", "status"}:
            return await self._combat_order(ctx)
        if sub == "start":
            names: list[str] = []
            hidden: set[str] = set()
            allies: set[str] = set()
            for piece in (piece.strip() for piece in rest.split(",")):
                marks = set()
                while piece and piece[0] in (_HIDDEN_MARK, _ALLY_MARK):
                    marks.add(piece[0])
                    piece = piece[1:].strip()
                if not piece:
                    continue
                names.append(piece)
                if _HIDDEN_MARK in marks:
                    hidden.add(piece)
                if _ALLY_MARK in marks:
                    allies.add(piece)
            if not names:
                return ctx.fail(ctx.i18n.t("combat.command.usage"))
            try:
                await start_room_encounter(ctx.services, agent_ctx, names, hidden=hidden, allies=allies)
            except CombatValidationError as exc:
                return ctx.fail(_start_failure(ctx, str(exc)))
        elif sub == "end":
            if not await end_room_encounter(ctx.services, agent_ctx):
                return ctx.fail(ctx.i18n.t("combat.command.none"))
            await self._combat_publish(ctx)
            return ctx.i18n.t("combat.command.ended")
        elif sub in {"hide", "reveal"}:
            if not rest or not await set_combatant_hidden(ctx.services, agent_ctx, rest, sub == "hide"):
                return ctx.fail(ctx.i18n.t("combat.command.not_found", name=rest))
        else:
            return ctx.fail(ctx.i18n.t("combat.command.usage"))
        await self._combat_publish(ctx)
        return await self._combat_order(ctx)

    async def _combat_publish(self, ctx: CommandCtx) -> None:
        if ctx.router.hub is not None:
            await publish_state(ctx.router.hub, ctx.services, ctx.raw_ctx)
