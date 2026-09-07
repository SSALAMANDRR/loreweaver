"""Hidden rich-client mutation lane for structured character creation.

The public `.create`, pack make-character word, and `.advance` handlers remain the
single mutation authority.  Rich clients need to invoke those same handlers without
putting their implementation commands and success prose into the narrative log, so
this adapter delegates to them on a shadow context and suppresses successful text.
Failures keep the underlying localized diagnostic.
"""

from __future__ import annotations

import copy

from core.rulepacks import load_rulepack, own_make_char_word
from gateway.commands.types import CommandCtx, CommandSpec

_CREATION_ACTION_WORD = "__creation_action"
_ACTIONS = frozenset({"start", "create", "advance"})


def _hidden_spec(handler) -> CommandSpec:  # noqa: ANN001
    return CommandSpec(
        canonical=_CREATION_ACTION_WORD,
        handler=handler,
        aliases_en=[_CREATION_ACTION_WORD],
        aliases_zh=[_CREATION_ACTION_WORD],
        slash=None,
        help_key="commands.help.create",
        private_reply=True,
    )


def _start_parts(raw: str) -> tuple[str, str, str] | None:
    parts = [part.strip() for part in raw.split("|", 2)]
    if not parts or not parts[0]:
        return None
    while len(parts) < 3:
        parts.append("")
    return parts[0], parts[1], parts[2]


class CreationActionCommands:
    """Delegate hidden GUI actions to the existing public deterministic handlers."""

    def resolve(self, text: str, locale: str):  # noqa: ANN201 - mirrors CommandRouter.resolve
        stripped = text.strip()
        prefix = next((item for item in self.prefixes if stripped.startswith(item)), "")
        if prefix:
            rest = stripped[len(prefix) :].lstrip()
            token, _separator, args = rest.partition(" ")
            if token.casefold() == _CREATION_ACTION_WORD:
                return _hidden_spec(self.cmd_creation_action), args.strip()
        return super().resolve(text, locale)

    async def cmd_creation_action(self, ctx: CommandCtx) -> str:
        action, separator, payload = ctx.args.strip().partition(" ")
        action = action.casefold()
        if not separator or action not in _ACTIONS:
            return ctx.fail(ctx.i18n.t("commands.error.bad_args"))

        shadow = copy.copy(ctx)
        # `copy.copy` intentionally shares the events list: if an underlying handler
        # ever emits a semantic event, it remains part of this command reply. Only its
        # human success prose is suppressed.
        shadow.failed = False

        if action == "start":
            parsed = _start_parts(payload)
            if parsed is None:
                return ctx.fail(ctx.i18n.t("commands.error.bad_args"))
            system, profile, name = parsed
            try:
                pack = load_rulepack(system)
                command = own_make_char_word(pack)
            except Exception:
                pack = None
                command = None
            if pack is None or command is None:
                return ctx.fail(ctx.i18n.t("commands.error.bad_args"))
            shadow.command = command
            if profile:
                shadow.args = f"{profile} | {name}" if name else profile
            else:
                shadow.args = name
            rendered = await self.cmd_make_char(shadow, pack)

        elif action == "create":
            shadow.command = "create"
            shadow.args = payload.strip()
            rendered = await self.cmd_create(shadow)

        else:  # advance
            shadow.command = "advance"
            shadow.args = payload.strip()
            rendered = await self.cmd_advance(shadow)

        ctx.failed = shadow.failed
        if shadow.failed:
            return ctx.fail(rendered)
        # Some legacy public handlers return the localized bad-arguments diagnostic
        # without marking their CommandCtx failed. The rich-client lane must not turn
        # that diagnostic into a silent success just because it suppresses prose.
        if rendered == ctx.i18n.t("commands.error.bad_args"):
            return ctx.fail(rendered)
        return ""
