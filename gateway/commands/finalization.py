"""Player-facing command for pack-declared character-creation finalization."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.character_manager import has_character
from core.creation_finalization import (
    CreationFinalizationError,
    CreationFinalizationStatus,
    FinalizationRow,
    creation_finalization_status,
    finalization_row,
    load_creation_finalization_spec,
    resolve_creation_finalization,
    roll_creation_finalization,
)
from core.creation_flow import creation_flow_status, load_creation_flow_spec
from core.rulepacks import RulePack, load_rulepack
from gateway.commands.types import CommandCtx, CommandSpec

_ROLL_WORDS = {"roll", "random", "投掷", "擲骰"}


def _localized(mapping: Mapping[str, str], locale: str, fallback: str) -> str:
    locale_key = str(locale or "en").replace("_", "-").casefold()
    language = locale_key.split("-", 1)[0]
    for key in (locale_key, language, "en"):
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return fallback


def _row_label(row: FinalizationRow, locale: str) -> str:
    return _localized(row.display, locale, row.id)


def _row_rules(row: FinalizationRow, locale: str) -> tuple[str, ...]:
    locale_key = str(locale or "en").replace("_", "-").casefold()
    language = locale_key.split("-", 1)[0]
    for key in (locale_key, language, "en"):
        rules = row.rules.get(key)
        if rules:
            return tuple(rules)
    return ()


def _option_label(raw: Mapping[str, Any], locale: str, fallback: str) -> str:
    display = raw.get("display") or {}
    if isinstance(display, Mapping):
        label = _localized(
            {str(key).casefold(): str(value) for key, value in display.items() if isinstance(value, str)},
            locale,
            "",
        )
        if label:
            return label
    names = raw.get("names") or []
    if isinstance(names, (list, tuple)):
        for name in names:
            if isinstance(name, str) and name.strip():
                return name.strip()
    return fallback


def _choice_lines(ctx: CommandCtx, row: FinalizationRow) -> tuple[list[str], str]:
    lines: list[str] = []
    assignments: list[str] = []
    for group_id_raw, group_raw in row.choices.items():
        group_id = str(group_id_raw)
        if not isinstance(group_raw, Mapping):
            continue
        if "field_template" in group_raw:
            lines.append(ctx.i18n.t("commands.finalization.choice_free", id=group_id))
            assignments.append(f"{group_id}=<value>")
            continue
        options = group_raw.get("options") or {}
        labels: list[str] = []
        if isinstance(options, Mapping):
            for option_id, raw in options.items():
                if isinstance(raw, Mapping):
                    labels.append(_option_label(raw, ctx.locale, str(option_id)))
        lines.append(
            ctx.i18n.t(
                "commands.finalization.choice",
                id=group_id,
                options=" / ".join(labels),
            )
        )
        assignments.append(f"{group_id}=<value>")
    return lines, " | ".join(assignments)


def _parse_assignments(raw: str) -> dict[str, str] | None:
    parts = [part.strip() for part in raw.split("|") if part.strip()]
    if not parts:
        return None
    values: dict[str, str] = {}
    for part in parts:
        key, separator, value = part.partition("=")
        key = key.strip()
        value = value.strip()
        if not separator or not key or not value or key in values:
            return None
        values[key] = value
    return values


def _render_status(
    ctx: CommandCtx,
    pack: RulePack,
    status: CreationFinalizationStatus | None,
) -> str:
    spec = load_creation_finalization_spec(pack)
    if spec is None:
        return ctx.i18n.t("commands.finalization.unsupported")
    if status is None:
        return ctx.i18n.t("commands.finalization.roll_prompt")

    row = finalization_row(spec, status.row_id)
    lines = [
        ctx.i18n.t(
            "commands.finalization.result",
            roll=status.roll,
            result=_row_label(row, ctx.locale),
        )
    ]
    for rule in _row_rules(row, ctx.locale):
        lines.append(ctx.i18n.t("commands.finalization.rule", rule=rule))

    if status.blocked_reference:
        lines.append(
            ctx.i18n.t(
                "commands.finalization.blocked",
                reference=status.blocked_reference,
            )
        )
        return "\n".join(lines)
    if status.complete:
        lines.append(ctx.i18n.t("commands.finalization.complete"))
        return "\n".join(lines)

    choice_lines, usage = _choice_lines(ctx, row)
    lines.extend(choice_lines)
    if usage:
        lines.append(ctx.i18n.t("commands.finalization.choice_usage", assignments=usage))
    return "\n".join(lines)


class FinalizationCommands:
    """Add a generic `.finalize` surface for pack-declared creation finales."""

    def _static_specs(self) -> list[CommandSpec]:
        specs = super()._static_specs()
        specs.append(
            CommandSpec(
                "finalize",
                self.cmd_finalize,
                ["finalize", "ready"],
                ["finalize", "ready", "定稿"],
                {"name": "finalize"},
                "commands.help.finalize",
                private_reply=True,
            )
        )
        return specs

    async def cmd_finalize(self, ctx: CommandCtx) -> str:
        character = await ctx.services.characters.get_character(ctx.user_id, ctx.chat_key)
        if not has_character(character):
            return ctx.fail(ctx.i18n.t("commands.finalization.no_character"))
        try:
            pack = load_rulepack(character.system)
        except Exception:
            return ctx.fail(ctx.i18n.t("commands.finalization.unsupported"))
        if load_creation_finalization_spec(pack) is None:
            return ctx.fail(ctx.i18n.t("commands.finalization.unsupported"))

        flow_spec = load_creation_flow_spec(pack)
        if flow_spec is not None:
            flow = creation_flow_status(pack, character)
            if flow is None or not flow.complete:
                return ctx.fail(ctx.i18n.t("commands.finalization.creation_incomplete"))

        status = creation_finalization_status(pack, character)
        raw = ctx.args.strip()
        if status is None:
            if not raw:
                return _render_status(ctx, pack, None)
            if raw.casefold() not in _ROLL_WORDS:
                return ctx.fail(ctx.i18n.t("commands.finalization.roll_prompt"))
            try:
                result = roll_creation_finalization(pack, character, roller=ctx.services.dice)
            except CreationFinalizationError:
                return ctx.fail(ctx.i18n.t("commands.finalization.invalid"))
            status = result.status
        elif status.complete or status.blocked_reference:
            return _render_status(ctx, pack, status)
        else:
            if not raw:
                return _render_status(ctx, pack, status)
            selections = _parse_assignments(raw)
            if selections is None:
                return ctx.fail(ctx.i18n.t("commands.finalization.invalid"))
            try:
                result = resolve_creation_finalization(pack, character, selections)
            except CreationFinalizationError:
                return ctx.fail(ctx.i18n.t("commands.finalization.invalid"))
            status = result.status

        try:
            await ctx.services.characters.save_character(ctx.user_id, ctx.chat_key, character)
        except Exception:
            return ctx.fail(ctx.i18n.t("commands.finalization.save_failed"))
        return _render_status(ctx, pack, status)
