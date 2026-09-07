"""Reconnect restoration for persisted manual physical-dice requests.

A pending manual roll lives in room state, so losing and re-establishing a client
connection must not make that request invisible.  This tiny hidden read-only
surface lets a rich client ask the engine to re-emit the already-persisted
``roll_request`` without creating a new request, consuming dice, or teaching the
transport anything about checks.
"""

from __future__ import annotations

from core.manual_roll import load_pending_roll
from gateway.commands.types import CommandCtx, CommandSpec
from gateway.hub import Event

_ROLL_PENDING_WORD = "__roll_pending"


def _hidden_spec(handler) -> CommandSpec:  # noqa: ANN001
    return CommandSpec(
        canonical=_ROLL_PENDING_WORD,
        handler=handler,
        aliases_en=[_ROLL_PENDING_WORD],
        aliases_zh=[_ROLL_PENDING_WORD],
        slash=None,
        help_key="commands.help.roll",
        private_reply=True,
    )


class ManualRollReconnectCommands:
    """Expose one private, read-only pending-roll refresh query to rich clients."""

    def resolve(self, text: str, locale: str):  # noqa: ANN201 - mirrors CommandRouter.resolve
        stripped = text.strip()
        prefix = next((item for item in self.prefixes if stripped.startswith(item)), "")
        if prefix:
            rest = stripped[len(prefix) :].lstrip()
            token, _separator, args = rest.partition(" ")
            if token.casefold() == _ROLL_PENDING_WORD:
                return _hidden_spec(self.cmd_manual_roll_pending), args.strip()
        return super().resolve(text, locale)

    async def cmd_manual_roll_pending(self, ctx: CommandCtx) -> str:
        """Re-emit this user's persisted pending request, if one still exists."""

        pending = await load_pending_roll(ctx.services.store, ctx.chat_key, ctx.user_id)
        if pending is not None:
            ctx.events.append(Event.panel(pending.wire(), private=True))
        # Service query: no prose belongs in the chronicle or UI. ``fail`` keeps
        # the existing command pipeline from manufacturing a normal success line.
        return ctx.fail("")
