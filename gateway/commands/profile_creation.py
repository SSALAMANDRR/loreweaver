"""Generic command wiring for pack-declared staged character creation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.advancement_purchase import initialize_advancement_budget
from core.advancement_surface import initialized_advancement_budget
from core.character_manager import CharacterNameTakenError, has_character
from core.character_rules import render_validation_notice, validate_sheet
from core.creation_flow import (
    CreationFlowError,
    CreationFlowStatus,
    apply_creation_flow_layer,
    choose_creation_flow_starting_item,
    creation_flow_duplicate_requirements,
    creation_flow_status,
    finish_creation_advancement,
    load_creation_flow_spec,
    resolve_creation_flow_duplicates,
    start_creation_flow,
)
from core.creation_layers import (
    CreationLayerError,
    load_creation_layers,
    resolve_creation_layer_option,
)
from core.creation_profiles import (
    CreationProfileError,
    generate_profiled_character,
    resolve_creation_profile,
)
from core.rulepacks import RulePack, load_rulepack
from core.starting_equipment import (
    StartingEquipmentError,
    available_starting_items,
    starting_equipment_budget,
)
from gateway.commands.types import CommandCtx, CommandSpec

_DONE_WORDS = {"done", "finish", "完成"}
_APPLY_WORDS = {"apply", "choose", "选择", "選擇"}


def _profile_map(pack: RulePack) -> Mapping[str, object]:
    raw = (pack.creation_constraints or {}).get("profiles") or {}
    return raw if isinstance(raw, Mapping) else {}


def _parse_profile_make_char_args(pack: RulePack, args: str, default_name: str) -> tuple[str, str]:
    """Parse ``<profile> [| <character name>]`` for a profiled make-char pack."""

    raw = args.strip()
    if not raw:
        raise CreationProfileError("a creation profile is required")

    profile_text, separator, name_text = raw.partition("|")
    if separator:
        resolved = resolve_creation_profile(pack, profile_text.strip())
        if resolved is None:
            raise CreationProfileError(f"unknown creation profile: {profile_text.strip()}")
        return resolved[0], name_text.strip() or default_name

    resolved = resolve_creation_profile(pack, raw)
    if resolved is not None:
        return resolved[0], default_name

    first, _space, rest = raw.partition(" ")
    resolved = resolve_creation_profile(pack, first)
    if resolved is not None:
        return resolved[0], rest.strip() or default_name

    raise CreationProfileError(f"unknown creation profile: {raw}")


def _stage(pack: RulePack, status: CreationFlowStatus):
    spec = load_creation_flow_spec(pack)
    if spec is None or status.complete or status.stage_index >= len(spec.stages):
        return None
    return spec.stages[status.stage_index]


def _option_label(raw: Mapping[str, Any], locale: str, fallback: str) -> str:
    display = raw.get("display") or {}
    if isinstance(display, Mapping):
        locale_key = locale.casefold()
        language = locale_key.split("-", 1)[0]
        for key in (locale_key, language, "en"):
            value = display.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    names = raw.get("names") or []
    if isinstance(names, (list, tuple)):
        for value in names:
            if isinstance(value, str) and value.strip():
                return value.strip()
    return fallback


def _layer_options(pack: RulePack, status: CreationFlowStatus) -> tuple[tuple[str, Mapping[str, Any]], ...]:
    stage = _stage(pack, status)
    if stage is None or stage.kind != "layer":
        return ()
    if stage.option_from_profile:
        resolved = resolve_creation_layer_option(pack, stage.layer_id, status.profile_id)
        return (resolved,) if resolved is not None else ()
    layers = load_creation_layers(pack)
    layer = layers.get(stage.layer_id)
    if not isinstance(layer, Mapping):
        return ()
    options = layer.get("options") or {}
    if not isinstance(options, Mapping):
        return ()
    return tuple(
        (str(option_id), raw)
        for option_id, raw in options.items()
        if isinstance(raw, Mapping)
    )


def _choice_groups(option: Mapping[str, Any]) -> Mapping[str, Any]:
    choices = option.get("choices") or {}
    return choices if isinstance(choices, Mapping) else {}


def _choice_option_labels(ctx: CommandCtx, group: Mapping[str, Any]) -> str:
    options = group.get("options") or {}
    if not isinstance(options, Mapping) or not options:
        return ""
    labels: list[str] = []
    for option_id, raw in options.items():
        if not isinstance(raw, Mapping):
            continue
        label = _option_label(raw, ctx.locale, str(option_id))
        if "skill_family" in raw or "field_template" in raw:
            label = ctx.i18n.t("commands.creation.choice_specialized", option=label)
        labels.append(label)
    return " / ".join(labels)


def _render_option_choices(ctx: CommandCtx, option: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    for group_id, raw in _choice_groups(option).items():
        if not isinstance(raw, Mapping):
            continue
        labels = _choice_option_labels(ctx, raw)
        if labels:
            lines.append(ctx.i18n.t("commands.creation.choice", id=group_id, options=labels))
        else:
            lines.append(ctx.i18n.t("commands.creation.choice_free", id=group_id))
    return lines


def _choice_usage(option: Mapping[str, Any]) -> str:
    return " | ".join(f"{group_id}=<value>" for group_id in _choice_groups(option))


def _render_creation_status(ctx: CommandCtx, pack: RulePack, character: Any) -> str:
    status = creation_flow_status(pack, character)
    if status is None:
        return ctx.i18n.t("commands.creation.not_started")
    if status.complete:
        return ctx.i18n.t("commands.creation.complete")

    stage = _stage(pack, status)
    if stage is None:
        return ctx.i18n.t("commands.creation.complete")
    lines = [ctx.i18n.t("commands.creation.header", stage=stage.id)]

    if stage.kind == "layer":
        options = _layer_options(pack, status)
        if stage.option_from_profile and options:
            option_id, raw = options[0]
            label = _option_label(raw, ctx.locale, option_id)
            lines.append(ctx.i18n.t("commands.creation.fixed_option", option=label))
            lines.extend(_render_option_choices(ctx, raw))
            usage = _choice_usage(raw)
            if usage:
                lines.append(ctx.i18n.t("commands.creation.fixed_usage", command=ctx.command, choices=usage))
            else:
                lines.append(ctx.i18n.t("commands.creation.fixed_apply", command=ctx.command))
            return "\n".join(lines)

        labels = [_option_label(raw, ctx.locale, option_id) for option_id, raw in options]
        lines.append(ctx.i18n.t("commands.creation.options", options=", ".join(labels)))
        lines.append(ctx.i18n.t("commands.creation.layer_browse", command=ctx.command))
        return "\n".join(lines)

    if stage.kind == "duplicates":
        requirements = creation_flow_duplicate_requirements(pack, character)
        assignments: list[str] = []
        for requirement in requirements:
            lines.append(
                ctx.i18n.t(
                    "commands.creation.duplicates",
                    field=requirement.field,
                    count=requirement.count,
                    choices=", ".join(requirement.choices),
                )
            )
            assignments.append(f"{requirement.field}=<value>; <value>")
        if assignments:
            lines.append(
                ctx.i18n.t(
                    "commands.creation.duplicates_usage",
                    command=ctx.command,
                    assignments=" | ".join(assignments),
                )
            )
        return "\n".join(lines)

    if stage.kind == "advancement":
        budget = initialized_advancement_budget(pack, character)
        if budget is not None:
            lines.append(
                ctx.i18n.t(
                    "commands.creation.advancement",
                    available=budget.available_xp,
                    spent=budget.spent_xp,
                    starting=budget.starting_xp,
                )
            )
        lines.append(ctx.i18n.t("commands.creation.advancement_usage", command=ctx.command))
        return "\n".join(lines)

    if stage.kind == "starting_equipment":
        budget = starting_equipment_budget(pack, character)
        items = available_starting_items(pack, character)
        if budget is not None:
            lines.append(
                ctx.i18n.t(
                    "commands.creation.equipment",
                    remaining=budget.remaining,
                    total=budget.total,
                    items=", ".join(item.name for item in items),
                )
            )
        lines.append(ctx.i18n.t("commands.creation.equipment_usage", command=ctx.command))
        return "\n".join(lines)

    return ctx.i18n.t("commands.creation.invalid")


def _parse_assignments(parts: list[str]) -> dict[str, str] | None:
    values: dict[str, str] = {}
    for part in parts:
        key, separator, value = part.partition("=")
        key = key.strip()
        value = value.strip()
        if not separator or not key or not value or key in values:
            return None
        values[key] = value
    return values


def _selection_value(group: Mapping[str, Any], raw: str) -> Any:
    options = group.get("options") or {}
    if isinstance(options, Mapping) and options and "::" in raw:
        option, specialization = raw.split("::", 1)
        if option.strip() and specialization.strip():
            return {"option": option.strip(), "specialization": specialization.strip()}
    return raw


def _parse_layer_action(
    pack: RulePack,
    status: CreationFlowStatus,
    raw: str,
) -> tuple[str | None, Mapping[str, Any] | None, Mapping[str, Any] | None]:
    stage = _stage(pack, status)
    if stage is None or stage.kind != "layer":
        return None, None, None
    parts = [part.strip() for part in raw.split("|") if part.strip()]
    options = _layer_options(pack, status)

    if stage.option_from_profile:
        if not options:
            return None, None, None
        _option_id, option_raw = options[0]
        if parts and parts[0].casefold() in _APPLY_WORDS:
            parts = parts[1:]
        assignments = _parse_assignments(parts)
        if assignments is None:
            return None, None, option_raw
        groups = _choice_groups(option_raw)
        if set(assignments) != {str(group_id) for group_id in groups}:
            return None, None, option_raw
        selections = {
            key: _selection_value(groups[key], value)
            for key, value in assignments.items()
            if isinstance(groups.get(key), Mapping)
        }
        return None, selections, option_raw

    if not parts:
        return None, None, None
    target = parts[0]
    resolved = resolve_creation_layer_option(pack, stage.layer_id, target)
    if resolved is None:
        return None, None, None
    _option_id, option_raw = resolved
    groups = _choice_groups(option_raw)
    assignments = _parse_assignments(parts[1:])
    if assignments is None or set(assignments) != {str(group_id) for group_id in groups}:
        return target, None, option_raw
    selections = {
        key: _selection_value(groups[key], value)
        for key, value in assignments.items()
        if isinstance(groups.get(key), Mapping)
    }
    return target, selections, option_raw


def _parse_duplicate_action(raw: str) -> Mapping[str, list[str]] | None:
    parts = [part.strip() for part in raw.split("|") if part.strip()]
    assignments = _parse_assignments(parts)
    if assignments is None:
        return None
    result: dict[str, list[str]] = {}
    for field, value in assignments.items():
        choices = [item.strip() for item in value.split(";") if item.strip()]
        if not choices:
            return None
        result[field] = choices
    return result


def _render_selected_layer(ctx: CommandCtx, target: str, option: Mapping[str, Any]) -> str:
    label = _option_label(option, ctx.locale, target)
    lines = [ctx.i18n.t("commands.creation.fixed_option", option=label)]
    lines.extend(_render_option_choices(ctx, option))
    usage = _choice_usage(option)
    if usage:
        lines.append(
            ctx.i18n.t(
                "commands.creation.layer_usage",
                command=ctx.command,
                choices=usage,
            )
        )
    return "\n".join(lines)


def _auto_apply_bound_layers(pack: RulePack, character: Any, roller: Any) -> CreationFlowStatus | None:
    status = creation_flow_status(pack, character)
    while status is not None and not status.complete:
        stage = _stage(pack, status)
        if stage is None or stage.kind != "layer" or not stage.option_from_profile:
            break
        options = _layer_options(pack, status)
        if not options or _choice_groups(options[0][1]):
            break
        status = apply_creation_flow_layer(pack, character, roller=roller).status
    return status


class ProfileCreationCommands:
    """Generic staged-creation command mixin plus profiled make-char entry point."""

    def _static_specs(self) -> list[CommandSpec]:
        specs = super()._static_specs()
        specs.append(
            CommandSpec(
                "create",
                self.cmd_create,
                ["create", "creation"],
                ["create", "creation", "创建", "創建"],
                {"name": "create"},
                "commands.help.create",
                private_reply=True,
            )
        )
        return specs

    async def cmd_make_char(self, ctx: CommandCtx, pack: RulePack | None = None) -> str:
        if pack is None:
            pack = await ctx.services.room_rulepack(ctx.raw_ctx)

        if not _profile_map(pack):
            return await super().cmd_make_char(ctx, pack)

        default_name = ctx.i18n.t("commands.character.default_name")
        budget = None
        flow_started = False
        try:
            profile_id, name = _parse_profile_make_char_args(pack, ctx.args, default_name)
            if load_creation_flow_spec(pack) is not None:
                started = start_creation_flow(
                    pack,
                    profile_id,
                    name,
                    roller=ctx.services.dice,
                )
                character = started.character
                _auto_apply_bound_layers(pack, character, ctx.services.dice)
                flow_started = True
            else:
                generated = generate_profiled_character(
                    pack,
                    profile_id,
                    name,
                    roller=ctx.services.dice,
                )
                character = generated.character
                budget = initialize_advancement_budget(pack, character)
        except (CreationProfileError, CreationFlowError, CreationLayerError):
            return ctx.i18n.t("commands.error.bad_args")

        character, violations = validate_sheet(
            character,
            pack.system,
            initialize_vitals=True,
            creation_method="rolled",
        )
        try:
            await ctx.services.characters.save_character(ctx.user_id, ctx.chat_key, character)
        except CharacterNameTakenError:
            return ctx.fail(
                ctx.i18n.t("commands.character.name_taken", name=character.name, command=ctx.command)
            )

        lines = [ctx.i18n.t("commands.character.created", name=character.name, system=character.system)]
        if flow_started:
            lines.append(ctx.i18n.t("commands.creation.started"))
            lines.append(_render_creation_status(ctx, pack, character))
        elif budget is not None:
            lines.append(
                ctx.i18n.t(
                    "commands.advancement.creation_hint",
                    available=budget.available_xp,
                )
            )
        notice = render_validation_notice(ctx.i18n, violations)
        if notice:
            lines.append(notice)
        return "\n".join(lines)

    async def cmd_create(self, ctx: CommandCtx) -> str:
        character = await ctx.services.characters.get_character(ctx.user_id, ctx.chat_key)
        if not has_character(character):
            return ctx.fail(ctx.i18n.t("commands.creation.no_character"))
        try:
            pack = load_rulepack(character.system)
        except Exception:
            return ctx.fail(ctx.i18n.t("commands.creation.unsupported"))
        if load_creation_flow_spec(pack) is None:
            return ctx.fail(ctx.i18n.t("commands.creation.unsupported"))

        status = creation_flow_status(pack, character)
        if status is None:
            return ctx.fail(ctx.i18n.t("commands.creation.not_started"))
        if status.complete:
            return ctx.i18n.t("commands.creation.complete")
        if not ctx.args.strip():
            return _render_creation_status(ctx, pack, character)

        stage = _stage(pack, status)
        if stage is None:
            return ctx.i18n.t("commands.creation.complete")

        try:
            if stage.kind == "layer":
                target, selections, option_raw = _parse_layer_action(pack, status, ctx.args.strip())
                if option_raw is None:
                    return ctx.fail(ctx.i18n.t("commands.creation.invalid"))
                groups = _choice_groups(option_raw)
                if selections is None and groups:
                    return _render_selected_layer(ctx, target or status.profile_id, option_raw)
                if selections is None:
                    selections = {}
                result = apply_creation_flow_layer(
                    pack,
                    character,
                    target,
                    selections=selections,
                    roller=ctx.services.dice,
                )
                _auto_apply_bound_layers(pack, character, ctx.services.dice)
                applied = _option_label(option_raw, ctx.locale, result.result.option_id)
                prefix = ctx.i18n.t("commands.creation.applied", option=applied)

            elif stage.kind == "duplicates":
                replacements = _parse_duplicate_action(ctx.args.strip())
                if replacements is None:
                    return ctx.fail(ctx.i18n.t("commands.creation.invalid"))
                resolve_creation_flow_duplicates(pack, character, replacements)
                prefix = ctx.i18n.t("commands.creation.duplicates_done")

            elif stage.kind == "advancement":
                if ctx.args.strip().casefold() not in _DONE_WORDS:
                    return ctx.fail(ctx.i18n.t("commands.creation.advancement_usage", command=ctx.command))
                finish_creation_advancement(pack, character)
                prefix = ctx.i18n.t("commands.creation.advancement_done")

            elif stage.kind == "starting_equipment":
                result = choose_creation_flow_starting_item(pack, character, ctx.args.strip())
                prefix = ctx.i18n.t(
                    "commands.creation.equipment_added",
                    item=result.grant.item.name,
                    remaining=result.grant.budget.remaining,
                )
            else:
                return ctx.fail(ctx.i18n.t("commands.creation.invalid"))

        except (CreationFlowError, CreationLayerError, StartingEquipmentError):
            return ctx.fail(ctx.i18n.t("commands.creation.invalid"))

        try:
            await ctx.services.characters.save_character(ctx.user_id, ctx.chat_key, character)
        except Exception:
            return ctx.fail(ctx.i18n.t("commands.creation.save_failed"))
        return f"{prefix}\n{_render_creation_status(ctx, pack, character)}"
