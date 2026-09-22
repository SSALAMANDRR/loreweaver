"""Small deterministic, pack-driven combat substrate.

The engine owns validation, resolution, and atomic state application. The
rulepack owns action costs, reaction allowances, reset semantics, hit locations,
and the other system-specific values consumed here.
"""
from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.check_outcome import CheckOutcome, RollDetail
from core.dice_engine import DiceRoller
from core.item_model import ItemInstance, ItemProfileCatalog
from core.sheets import sheet_value
from core.yaml_safety import safe_load_no_aliases


class CombatValidationError(ValueError):
    """The declared combat operation cannot be executed."""


@dataclass
class CombatantState:
    """Mutable per-combatant counters; limits come from rulepack data."""

    action_budget: int
    action_budget_max: int
    reactions_remaining: int
    reactions_max: int


@dataclass
class CombatState:
    """Minimal round/turn state, deliberately not a full encounter manager."""

    round_number: int
    current_actor: str
    combatants: dict[str, CombatantState] = field(default_factory=dict)


@dataclass(frozen=True)
class ActionCost:
    """Generic action-cost declaration compiled from rulepack data."""

    amount: int


@dataclass(frozen=True)
class _ReactionContract:
    kind: str
    cost: int
    spend_on: str
    target_value: str | None


@dataclass(frozen=True)
class _ActionContract:
    cost: ActionCost
    attack_value: str
    damage_value: str
    toughness_value: str
    damage_bonus_value: str | None
    weapon_profiles: tuple[str, ...]
    consumes_ammo: bool
    reaction: _ReactionContract | None


@dataclass(frozen=True)
class _CombatContract:
    action_budget_per_turn: int
    reactions_per_round: int
    reaction_reset: str


@dataclass(frozen=True)
class ActionRequest:
    actor: Any
    target: Any
    weapon_instance_id: str
    mode: str = "single"
    attack_roll: int | None = None
    location_roll: int | None = None
    damage_roll: int | None = None
    reaction_roll: int | None = None
    reaction_target: int | None = None


@dataclass(frozen=True)
class StateDelta:
    """Expected-before/desired-after values for one atomic combat mutation."""

    combat_state_before: CombatState
    combat_state_after: CombatState
    action_cost: int = 0
    reaction_cost: int = 0
    ammo_before: int | None = None
    ammo_after: int | None = None
    target_damage_before: int | None = None
    target_damage_after: int | None = None
    weapon_instance_id: str | None = None


@dataclass(frozen=True)
class CombatResult:
    actor: str
    target: str
    action: str
    weapon_instance_id: str
    weapon_profile_id: str
    attack_target: int | None = None
    attack_roll: int | None = None
    success: bool = False
    margin: int | None = None
    degrees: int | None = None
    hit_location: str | None = None
    reaction: Mapping[str, Any] | None = None
    raw_damage: int | None = None
    penetration: int | None = None
    armour_before: int | None = None
    armour_after_penetration: int | None = None
    tb_reduction: int | None = None
    final_damage: int = 0
    ammo_before: int | None = None
    ammo_after: int | None = None
    state_delta: StateDelta | None = None
    validation_failure: str | None = None

    @property
    def ok(self) -> bool:
        return self.validation_failure is None


def _data_path(pack: Any) -> Path:
    root = Path(__file__).resolve().parent.parent / "rulepacks" / "data"
    return root / str(pack.system) / "combat.yaml"


def _combat_data(pack: Any) -> Mapping[str, Any]:
    path = _data_path(pack)
    if not path.is_file():
        raise CombatValidationError(f"no combat data for rulepack {pack.system!r}")
    raw = safe_load_no_aliases(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, Mapping) or int(raw.get("version", 1)) != 1:
        raise CombatValidationError("unsupported combat data")
    return raw


def _required_int(value: Any, *, where: str, minimum: int = 0) -> int:
    if isinstance(value, bool):
        raise CombatValidationError(f"{where} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise CombatValidationError(f"{where} must be an integer") from exc
    if parsed < minimum:
        raise CombatValidationError(f"{where} must be at least {minimum}")
    return parsed


def _contract(pack: Any) -> _CombatContract:
    raw = _combat_data(pack).get("turn_state")
    if not isinstance(raw, Mapping):
        raise CombatValidationError("combat turn-state contract is missing")  # i18n-exempt: internal validation diagnostic
    reset = str(raw.get("reaction_reset") or "").strip()
    if reset != "round":
        raise CombatValidationError("unsupported reaction reset semantics")
    return _CombatContract(
        action_budget_per_turn=_required_int(
            raw.get("action_budget_per_turn"),
            where="turn_state.action_budget_per_turn",
            minimum=1,
        ),
        reactions_per_round=_required_int(
            raw.get("reactions_per_round"), where="turn_state.reactions_per_round"
        ),
        reaction_reset=reset,
    )


def _action_contract(pack: Any, action: str, mode: str) -> _ActionContract:
    actions = _combat_data(pack).get("actions")
    action_raw = actions.get(action) if isinstance(actions, Mapping) else None
    modes = action_raw.get("modes") if isinstance(action_raw, Mapping) else None
    mode_raw = modes.get(mode) if isinstance(modes, Mapping) else None
    if not isinstance(mode_raw, Mapping):
        raise CombatValidationError(f"combat action {action!r} mode {mode!r} is not declared")
    cost = ActionCost(_required_int(mode_raw.get("action_cost"), where="action_cost", minimum=1))
    value_names = {
        field_name: str(mode_raw.get(field_name) or "").strip()
        for field_name in ("attack_value", "damage_value", "toughness_value")
    }
    if not all(value_names.values()):
        raise CombatValidationError("action sheet-value contract is incomplete")  # i18n-exempt: internal validation diagnostic
    profiles_raw = mode_raw.get("weapon_profiles")
    if not isinstance(profiles_raw, list) or not profiles_raw or not all(
        isinstance(profile, str) and profile.strip() for profile in profiles_raw
    ):
        raise CombatValidationError("action weapon-profile contract is incomplete")  # i18n-exempt: internal validation diagnostic
    extra_values = {
        "damage_bonus_value": (
            str(mode_raw["damage_bonus_value"]).strip()
            if mode_raw.get("damage_bonus_value") is not None
            else None
        ),
        "weapon_profiles": tuple(profile.strip() for profile in profiles_raw),
        "consumes_ammo": bool(mode_raw.get("consumes_ammo", False)),
    }
    reaction_raw = mode_raw.get("reaction")
    if reaction_raw is None:
        return _ActionContract(cost=cost, reaction=None, **value_names, **extra_values)
    if not isinstance(reaction_raw, Mapping):
        raise CombatValidationError("reaction contract must be a mapping")  # i18n-exempt: internal validation diagnostic
    spend_on = str(reaction_raw.get("spend_on") or "").strip()
    if spend_on != "attempt":
        raise CombatValidationError("unsupported reaction spend semantics")
    kind = str(reaction_raw.get("type") or "").strip()
    if not kind:
        raise CombatValidationError("reaction type is required")
    return _ActionContract(
        cost=cost,
        reaction=_ReactionContract(
            kind=kind,
            cost=_required_int(reaction_raw.get("cost", 1), where="reaction.cost", minimum=1),
            spend_on=spend_on,
            target_value=(
                str(reaction_raw["target_value"]).strip()
                if reaction_raw.get("target_value") is not None
                else None
            ),
        ),
        **value_names,
        **extra_values,
    )


def create_combat_state(
    combatants: Iterable[str], *, current_actor: str, pack: Any, round_number: int = 1
) -> CombatState:
    """Create the initial state from a rulepack's minimal turn contract."""
    contract = _contract(pack)
    names = [str(name).strip() for name in combatants]
    if not names or any(not name for name in names) or len(set(names)) != len(names):
        raise CombatValidationError("combatant names must be non-empty and unique")  # i18n-exempt: internal validation diagnostic
    if current_actor not in names:
        raise CombatValidationError("current actor is not a combatant")  # i18n-exempt: internal validation diagnostic
    parsed_round = _required_int(round_number, where="round_number", minimum=1)
    states = {
        name: CombatantState(
            action_budget=contract.action_budget_per_turn,
            action_budget_max=contract.action_budget_per_turn,
            reactions_remaining=contract.reactions_per_round,
            reactions_max=contract.reactions_per_round,
        )
        for name in names
    }
    return CombatState(parsed_round, current_actor, states)


def resolve_turn_transition(
    state: CombatState, *, next_actor: str, pack: Any, round_number: int | None = None
) -> StateDelta:
    """Build, but do not apply, a turn or one-round transition."""
    contract = _contract(pack)
    if next_actor not in state.combatants:
        raise CombatValidationError("next actor is not a combatant")  # i18n-exempt: internal validation diagnostic
    next_round = state.round_number if round_number is None else int(round_number)
    if next_round not in {state.round_number, state.round_number + 1}:
        raise CombatValidationError("combat round transition must stay in or advance one round")  # i18n-exempt: internal validation diagnostic
    if next_round == state.round_number and next_actor == state.current_actor:
        raise CombatValidationError("same-round transition must advance to another actor")  # i18n-exempt: internal validation diagnostic
    before = copy.deepcopy(state)
    after = copy.deepcopy(state)
    after.round_number = next_round
    after.current_actor = next_actor
    next_state = after.combatants[next_actor]
    next_state.action_budget = contract.action_budget_per_turn
    next_state.action_budget_max = contract.action_budget_per_turn
    if next_round > state.round_number and contract.reaction_reset == "round":
        for combatant in after.combatants.values():
            combatant.reactions_remaining = contract.reactions_per_round
            combatant.reactions_max = contract.reactions_per_round
    return StateDelta(before, after)


def apply_combat_state_delta(state: CombatState, delta: StateDelta) -> None:
    """Atomically apply a state-only transition delta."""
    if any(
        value is not None
        for value in (
            delta.ammo_before,
            delta.ammo_after,
            delta.target_damage_before,
            delta.target_damage_after,
            delta.weapon_instance_id,
        )
    ):
        raise CombatValidationError("combat-state transition delta contains entity changes")  # i18n-exempt: internal validation diagnostic
    if state != delta.combat_state_before:
        raise CombatValidationError("stale combat state")
    _replace_combat_state(state, delta.combat_state_after)


def _location(pack: Any, roll: int) -> str:
    value = max(1, min(100, int(roll)))
    for row in _combat_data(pack).get("hit_locations", []):
        if value <= int(row["max"]):
            return str(row["id"])
    raise CombatValidationError("hit location table has no matching row")  # i18n-exempt: internal validation diagnostic


def _roll_detail(value: int) -> RollDetail:
    return RollDetail(expression="1d100", dice=(int(value),), total=int(value))


def _dice_expression(expression: str) -> str:
    # Pack data may use the Cyrillic ``к`` separator in localized dice notation.
    return str(expression).replace("к", "d").replace("К", "d")


def _name(sheet: Any) -> str:
    return str(getattr(sheet, "name", "") or "")


def _find_weapon(actor: Any, request: ActionRequest) -> ItemInstance:
    for item in getattr(actor, "equipment", []) or []:
        if isinstance(item, ItemInstance) and item.instance_id == request.weapon_instance_id:
            return item
    raise CombatValidationError("weapon instance is missing")


def _profiles(actor: Any, pack: Any) -> ItemProfileCatalog:
    from core.item_model import load_item_catalog

    catalog = load_item_catalog(pack)
    if catalog is None:
        raise CombatValidationError("item profile catalog is missing")
    return catalog


def _combatant(state: CombatState, name: str) -> CombatantState:
    combatant = state.combatants.get(name)
    if combatant is None:
        raise CombatValidationError(f"combatant {name!r} is missing from combat state")
    return combatant


def _resolve_attack(
    request: ActionRequest,
    *,
    action: str,
    combat_state: CombatState,
    pack: Any | None = None,
    dice: DiceRoller | None = None,
) -> CombatResult:
    """Resolve one pack-declared attack without mutating entity or combat state."""
    try:
        if request.actor is None or request.target is None:
            raise CombatValidationError("actor and target are required")
        if pack is None:
            from core.rulepacks import load_rulepack

            pack = load_rulepack(getattr(request.actor, "system", ""))
        if getattr(request.actor, "system", "") != getattr(request.target, "system", ""):
            raise CombatValidationError("actor and target must use the same rulepack")  # i18n-exempt: internal validation diagnostic
        actor_name = _name(request.actor)
        target_name = _name(request.target)
        if combat_state.current_actor != actor_name:
            raise CombatValidationError("actor does not have the current turn")  # i18n-exempt: internal validation diagnostic
        action_contract = _action_contract(pack, action, request.mode)
        actor_state = _combatant(combat_state, actor_name)
        if actor_state.action_budget < action_contract.cost.amount:
            raise CombatValidationError("insufficient action budget")
        reaction_requested = request.reaction_roll is not None or request.reaction_target is not None
        if reaction_requested:
            if action_contract.reaction is None:
                raise CombatValidationError("action does not allow a reaction")  # i18n-exempt: internal validation diagnostic
            if (
                request.reaction_target is None
                and action_contract.reaction.target_value is None
            ):
                raise CombatValidationError("reaction roll requires a reaction target")  # i18n-exempt: internal validation diagnostic
            target_state = _combatant(combat_state, target_name)
            if target_state.reactions_remaining < action_contract.reaction.cost:
                raise CombatValidationError("reaction is unavailable")

        weapon = _find_weapon(request.actor, request)
        catalog = _profiles(request.actor, pack)
        profile = catalog.get(weapon.profile_id)
        if profile is None or profile.kind != "weapon":
            raise CombatValidationError("item is not a weapon profile")  # i18n-exempt: internal validation diagnostic
        if profile.id not in action_contract.weapon_profiles:
            raise CombatValidationError("weapon does not support the declared action")  # i18n-exempt: internal validation diagnostic
        if action_contract.consumes_ammo:
            if request.mode not in profile.rate_of_fire or profile.rate_of_fire[request.mode] is None:
                raise CombatValidationError("weapon does not support the declared fire mode")  # i18n-exempt: internal validation diagnostic
            if weapon.current_ammo is None:
                raise CombatValidationError("weapon ammo state is missing")
            if weapon.current_ammo < 1:
                raise CombatValidationError("out of ammunition")
        if pack.resolver is None:
            raise CombatValidationError("rulepack has no check resolver")

        state_before = copy.deepcopy(combat_state)
        state_after = copy.deepcopy(combat_state)
        state_after.combatants[actor_name].action_budget -= action_contract.cost.amount
        reaction_cost = 0
        ammo_before = weapon.current_ammo if action_contract.consumes_ammo else None
        target_value = sheet_value(request.actor, pack, action_contract.attack_value)
        roller = dice or DiceRoller()
        attack = (
            _roll_detail(request.attack_roll)
            if request.attack_roll is not None
            else roller.roll_detail(pack.resolver.roll)
        )
        outcome: CheckOutcome = pack.resolver.interpret(attack, target_value)
        success = bool(outcome.rank.success)
        location = None
        reaction: dict[str, Any] | None = None
        raw_damage = penetration = armour = armour_after = tb = final = None
        damage_before = sheet_value(request.target, pack, action_contract.damage_value)
        damage_after = damage_before
        if success:
            location_roll = request.location_roll if request.location_roll is not None else attack.total
            location = _location(pack, location_roll)
            if reaction_requested:
                target_state = _combatant(state_after, target_name)
                rr = (
                    request.reaction_roll
                    if request.reaction_roll is not None
                    else roller.roll_detail("1d100").total
                )
                reaction_target = (
                    sheet_value(request.target, pack, action_contract.reaction.target_value)
                    if action_contract.reaction.target_value is not None
                    else request.reaction_target
                )
                reaction_outcome = pack.resolver.interpret(
                    _roll_detail(rr), reaction_target
                )
                reaction_success = bool(reaction_outcome.rank.success)
                reaction_cost = action_contract.reaction.cost
                target_state.reactions_remaining -= reaction_cost
                reaction = {
                    "type": action_contract.reaction.kind,
                    "roll": rr,
                    "target": reaction_target,
                    "success": reaction_success,
                    "prevents_hit": reaction_success,
                    "spent": True,
                }
                if reaction_success:
                    success = False
            if success:
                if profile.damage_expression is None or profile.penetration is None:
                    raise CombatValidationError("weapon damage profile is incomplete")
                raw_damage = (
                    request.damage_roll
                    if request.damage_roll is not None
                    else roller.roll_expression(_dice_expression(profile.damage_expression)).total
                )
                if action_contract.damage_bonus_value is not None:
                    raw_damage += sheet_value(
                        request.actor, pack, action_contract.damage_bonus_value
                    )
                penetration = int(profile.penetration)
                armour = 0
                for item in getattr(request.target, "equipment", []) or []:
                    if isinstance(item, ItemInstance):
                        armour_profile = catalog.get(item.profile_id)
                        if armour_profile is not None and armour_profile.kind == "armour":
                            armour = max(armour, armour_profile.armor_at(location))
                armour_after = max(0, armour - penetration)
                tb = sheet_value(request.target, pack, action_contract.toughness_value)
                final = max(0, int(raw_damage) - armour_after - tb)
                damage_after = damage_before + final
        delta = StateDelta(
            combat_state_before=state_before,
            combat_state_after=state_after,
            action_cost=action_contract.cost.amount,
            reaction_cost=reaction_cost,
            ammo_before=ammo_before,
            ammo_after=(ammo_before - 1 if ammo_before is not None else None),
            target_damage_before=damage_before,
            target_damage_after=damage_after,
            weapon_instance_id=weapon.instance_id,
        )
        degrees = (abs(outcome.margin) // 10 + 1) if outcome.margin is not None else None
        return CombatResult(
            actor_name,
            target_name,
            action,
            weapon.instance_id,
            profile.id,
            target_value,
            attack.total,
            success,
            outcome.margin,
            degrees,
            location,
            reaction,
            raw_damage,
            penetration,
            armour,
            armour_after,
            tb,
            final or 0,
            ammo_before,
            ammo_before - 1 if ammo_before is not None else None,
            delta,
        )
    except CombatValidationError as exc:
        return CombatResult(
            _name(request.actor),
            _name(request.target),
            action,
            request.weapon_instance_id,
            "",
            validation_failure=str(exc),
        )


def resolve_first_shot(
    request: ActionRequest,
    *,
    combat_state: CombatState,
    pack: Any | None = None,
    dice: DiceRoller | None = None,
) -> CombatResult:
    """Resolve one pack-declared single shot without mutating entity or combat state."""
    return _resolve_attack(
        request,
        action="ranged_attack",
        combat_state=combat_state,
        pack=pack,
        dice=dice,
    )


def resolve_melee_attack(
    request: ActionRequest,
    *,
    combat_state: CombatState,
    pack: Any | None = None,
    dice: DiceRoller | None = None,
) -> CombatResult:
    """Resolve one pack-declared basic melee attack without mutating state."""
    return _resolve_attack(
        request,
        action="melee_attack",
        combat_state=combat_state,
        pack=pack,
        dice=dice,
    )


def _replace_combat_state(target: CombatState, source: CombatState) -> None:
    target.round_number = source.round_number
    target.current_actor = source.current_actor
    target.combatants = copy.deepcopy(source.combatants)


def apply_state_delta(
    request: ActionRequest,
    result: CombatResult,
    *,
    combat_state: CombatState,
    pack: Any | None = None,
) -> None:
    """Apply entity and combat-state changes together, rolling all back on failure."""
    if not result.ok or result.state_delta is None:
        raise CombatValidationError("cannot apply invalid combat result")
    delta = result.state_delta
    weapon = _find_weapon(request.actor, request)
    if delta.weapon_instance_id != weapon.instance_id:
        raise CombatValidationError("combat action delta has the wrong weapon")  # i18n-exempt: internal validation diagnostic
    if delta.ammo_before is not None and weapon.current_ammo != delta.ammo_before:
        raise CombatValidationError("stale weapon state")
    if combat_state != delta.combat_state_before:
        raise CombatValidationError("stale combat state")
    if pack is None:
        from core.rulepacks import load_rulepack

        pack = load_rulepack(request.target.system)
    action_contract = _action_contract(pack, result.action, request.mode)
    if sheet_value(request.target, pack, action_contract.damage_value) != delta.target_damage_before:
        raise CombatValidationError("stale target damage state")
    attributes_before = copy.deepcopy(request.target.attributes)
    weapon_state_before = copy.deepcopy(weapon.state)
    combat_before = copy.deepcopy(combat_state)
    try:
        if delta.ammo_before is not None:
            weapon.state["current_ammo"] = delta.ammo_after
        request.target.attributes[action_contract.damage_value] = delta.target_damage_after
        _replace_combat_state(combat_state, delta.combat_state_after)
    except Exception:
        request.target.attributes.clear()
        request.target.attributes.update(attributes_before)
        weapon.state.clear()
        weapon.state.update(weapon_state_before)
        _replace_combat_state(combat_state, combat_before)
        raise
