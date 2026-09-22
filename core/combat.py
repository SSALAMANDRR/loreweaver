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
    aim_bonus: int = 0
    aimed_weapon_instance_id: str | None = None


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
    hits_avoided_per_degree: int | None = None


@dataclass(frozen=True)
class _ActionContract:
    cost: ActionCost
    attack_value: str
    damage_value: str
    toughness_value: str
    damage_bonus_value: str | None
    weapon_profiles: tuple[str, ...]
    consumes_ammo: bool
    reactions: Mapping[str, _ReactionContract]
    attack_modifier: int = 0
    extra_hit_degrees: int | None = None


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
    reaction_type: str | None = None
    distance: int | None = None
    damage_rolls: tuple[int, ...] = ()
    location_rolls: tuple[int, ...] = ()


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
    hits: tuple[HitResult, ...] = ()
    shots_fired: int = 0

    @property
    def ok(self) -> bool:
        return self.validation_failure is None


@dataclass(frozen=True)
class HitResult:
    """One resolved impact; mitigation is calculated independently per hit."""

    location: str
    raw_damage: int
    penetration: int
    armour_before: int
    armour_after_penetration: int
    tb_reduction: int
    final_damage: int


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
    if action in {"ranged_attack", "melee_attack"} and not all(value_names.values()):
        raise CombatValidationError("action sheet-value contract is incomplete")  # i18n-exempt: internal validation diagnostic
    profiles_raw = mode_raw.get("weapon_profiles")
    if action in {"ranged_attack", "melee_attack"} and (not isinstance(profiles_raw, list) or not profiles_raw or not all(
        isinstance(profile, str) and profile.strip() for profile in profiles_raw
    )):
        raise CombatValidationError("action weapon-profile contract is incomplete")  # i18n-exempt: internal validation diagnostic
    extra_values = {
        "damage_bonus_value": (
            str(mode_raw["damage_bonus_value"]).strip()
            if mode_raw.get("damage_bonus_value") is not None
            else None
        ),
        "weapon_profiles": tuple(profile.strip() for profile in (profiles_raw or [])),
        "consumes_ammo": bool(mode_raw.get("consumes_ammo", False)),
        "attack_modifier": _required_int(mode_raw.get("attack_modifier", 0), where="attack_modifier", minimum=-100),
        "extra_hit_degrees": (
            _required_int(mode_raw["extra_hit_degrees"], where="extra_hit_degrees", minimum=1)
            if mode_raw.get("extra_hit_degrees") is not None else None
        ),
    }
    if extra_values["extra_hit_degrees"] is not None and _combat_data(pack).get("margin_unit") != "degrees":
        raise CombatValidationError("multi-hit action requires degree margin")  # i18n-exempt: internal validation diagnostic
    reactions_raw = mode_raw.get("reactions")
    if reactions_raw is None and mode_raw.get("reaction") is not None:
        legacy = mode_raw["reaction"]
        reactions_raw = {legacy.get("type"): legacy} if isinstance(legacy, Mapping) else legacy
    if reactions_raw is None:
        reactions_raw = {}
    if not isinstance(reactions_raw, Mapping):
        raise CombatValidationError("reaction contract must be a mapping")  # i18n-exempt: internal validation diagnostic
    reactions = {}
    for kind, raw in reactions_raw.items():
        if not isinstance(raw, Mapping) or raw.get("spend_on") != "attempt":
            raise CombatValidationError("unsupported reaction spend semantics")
        reactions[str(kind)] = _ReactionContract(
            kind=str(kind),
            cost=_required_int(raw.get("cost", 1), where="reaction.cost", minimum=1),
            spend_on="attempt",
            target_value=str(raw["target_value"]).strip() if raw.get("target_value") else None,
            hits_avoided_per_degree=(
                _required_int(raw["hits_avoided_per_degree"], where="reaction hits avoided", minimum=1)
                if raw.get("hits_avoided_per_degree") is not None else None
            ),
        )
    return _ActionContract(cost=cost, reactions=reactions, **value_names, **extra_values)


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
    value = _roll_detail(roll).total
    for row in _combat_data(pack).get("hit_locations", []):
        if value <= int(row["max"]):
            return str(row["id"])
    raise CombatValidationError("hit location table has no matching row")  # i18n-exempt: internal validation diagnostic


def _location_roll(pack: Any, attack_roll: int) -> int:
    transform = _combat_data(pack).get("hit_location_roll")
    if transform == "reverse_digits":
        reversed_roll = int(f"{attack_roll % 100:02d}"[::-1])
        return reversed_roll or 100
    if transform == "attack_roll":
        return attack_roll
    raise CombatValidationError("unsupported hit location roll transform")


def _roll_detail(value: int) -> RollDetail:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
        raise CombatValidationError("percentile roll must be between 1 and 100")  # i18n-exempt: internal validation diagnostic
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


def _reaction_contract(contract: _ActionContract, request: ActionRequest) -> _ReactionContract | None:
    requested = request.reaction_type
    if requested is None and (request.reaction_roll is not None or request.reaction_target is not None):
        if request.reaction_target is not None and "basic" in contract.reactions:
            requested = "basic"
        elif request.reaction_target is None and "dodge" in contract.reactions and "parry" not in contract.reactions:
            requested = "dodge"
        else:
            requested = next(iter(contract.reactions), None)
    if requested is None:
        return None
    reaction = contract.reactions.get(requested)
    if reaction is None:
        raise CombatValidationError("action does not allow a reaction")  # i18n-exempt: internal validation diagnostic
    if reaction.target_value is None and request.reaction_target is None:
        raise CombatValidationError("reaction roll requires a reaction target")  # i18n-exempt: internal validation diagnostic
    return reaction


def _range_modifier(pack: Any, distance: int | None, profile: Any) -> int:
    if distance is None:
        return 0
    if isinstance(distance, bool) or not isinstance(distance, int) or distance < 0:
        raise CombatValidationError("distance must be a non-negative integer")  # i18n-exempt: internal validation diagnostic
    if not isinstance(profile.range, int) or profile.range < 1:
        raise CombatValidationError("weapon range is unavailable")
    ratio = distance / profile.range
    for band in _combat_data(pack).get("range_bands", []):
        if "max_distance" in band and distance <= int(band["max_distance"]):
            return _required_int(band["modifier"], where="range modifier", minimum=-100)
        if "max_multiple" in band and ratio <= float(band["max_multiple"]):
            return _required_int(band["modifier"], where="range modifier", minimum=-100)
    raise CombatValidationError("target is out of weapon range")  # i18n-exempt: internal validation diagnostic


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
        reaction_contract = _reaction_contract(action_contract, request)
        if reaction_contract is not None:
            target_state = _combatant(combat_state, target_name)
            if target_state.reactions_remaining < reaction_contract.cost:
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
            shots_fired = int(profile.rate_of_fire[request.mode])
            if weapon.current_ammo < shots_fired:
                raise CombatValidationError("out of ammunition")
        else:
            shots_fired = 0
        if action == "ranged_attack":
            range_modifier = _range_modifier(pack, request.distance, profile)
        elif request.distance is not None:
            raise CombatValidationError("distance is unavailable for this action")  # i18n-exempt: internal validation diagnostic
        else:
            range_modifier = 0
        if pack.resolver is None:
            raise CombatValidationError("rulepack has no check resolver")

        state_before = copy.deepcopy(combat_state)
        state_after = copy.deepcopy(combat_state)
        state_after.combatants[actor_name].action_budget -= action_contract.cost.amount
        reaction_cost = 0
        ammo_before = weapon.current_ammo if action_contract.consumes_ammo else None
        aim_bonus = actor_state.aim_bonus if actor_state.aimed_weapon_instance_id == weapon.instance_id else 0
        target_value = (
            sheet_value(request.actor, pack, action_contract.attack_value)
            + action_contract.attack_modifier + range_modifier + aim_bonus
        )
        state_after.combatants[actor_name].aim_bonus = 0
        state_after.combatants[actor_name].aimed_weapon_instance_id = None
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
        hits: list[HitResult] = []
        # The compiled resolver's margin is pack-defined; DH2 declares it in
        # degrees already, so the combat contract consumes that value directly.
        degrees = abs(outcome.margin) if outcome.margin is not None else 1
        if success:
            location_roll = request.location_roll if request.location_roll is not None else _location_roll(pack, attack.total)
            location = _location(pack, location_roll)
            hit_count = 1
            if action_contract.extra_hit_degrees is not None:
                hit_count = min(shots_fired, 1 + max(0, degrees - 1) // action_contract.extra_hit_degrees)
            if reaction_contract is not None:
                target_state = _combatant(state_after, target_name)
                rr = (
                    request.reaction_roll
                    if request.reaction_roll is not None
                    else roller.roll_detail("1d100").total
                )
                reaction_target = (
                    sheet_value(request.target, pack, reaction_contract.target_value)
                    if reaction_contract.target_value is not None
                    else request.reaction_target
                )
                reaction_outcome = pack.resolver.interpret(_roll_detail(rr), reaction_target)
                reaction_success = bool(reaction_outcome.rank.success)
                reaction_cost = reaction_contract.cost
                target_state.reactions_remaining -= reaction_cost
                reaction = {
                    "type": reaction_contract.kind,
                    "roll": rr,
                    "target": reaction_target,
                    "success": reaction_success,
                    "prevents_hit": False,
                    "spent": True,
                }
                if reaction_success:
                    avoided = hit_count
                    if reaction_contract.hits_avoided_per_degree is not None:
                        reaction_degrees = abs(reaction_outcome.margin) if reaction_outcome.margin is not None else 1
                        avoided = min(hit_count, reaction_degrees * reaction_contract.hits_avoided_per_degree)
                        reaction["hits_avoided"] = avoided
                    hit_count -= avoided
                    success = hit_count > 0
                    reaction["prevents_hit"] = hit_count == 0
            if hit_count:
                if profile.damage_expression is None or profile.penetration is None:
                    raise CombatValidationError("weapon damage profile is incomplete")
                if request.damage_rolls and len(request.damage_rolls) != hit_count:
                    raise CombatValidationError("damage rolls do not match hit count")  # i18n-exempt: internal validation diagnostic
                if request.location_rolls and len(request.location_rolls) != hit_count:
                    raise CombatValidationError("location rolls do not match hit count")  # i18n-exempt: internal validation diagnostic
                for index in range(hit_count):
                    hit_location = _location(pack, request.location_rolls[index]) if request.location_rolls else location
                    hit_damage = (
                        request.damage_rolls[index] if request.damage_rolls else
                        request.damage_roll if index == 0 and request.damage_roll is not None else
                        roller.roll_expression(_dice_expression(profile.damage_expression)).total
                    )
                    if isinstance(hit_damage, bool) or not isinstance(hit_damage, int) or hit_damage < 0:
                        raise CombatValidationError("damage roll must be non-negative")  # i18n-exempt: internal validation diagnostic
                    if action_contract.damage_bonus_value is not None:
                        hit_damage += sheet_value(request.actor, pack, action_contract.damage_bonus_value)
                    hit_armour = 0
                    for item in getattr(request.target, "equipment", []) or []:
                        if isinstance(item, ItemInstance):
                            armour_profile = catalog.get(item.profile_id)
                            if armour_profile is not None and armour_profile.kind == "armour":
                                hit_armour = max(hit_armour, armour_profile.armor_at(hit_location))
                    hit_penetration = int(profile.penetration)
                    reduced_armour = max(0, hit_armour - hit_penetration)
                    hit_tb = sheet_value(request.target, pack, action_contract.toughness_value)
                    hit_final = max(0, int(hit_damage) - reduced_armour - hit_tb)
                    hits.append(HitResult(hit_location, hit_damage, hit_penetration, hit_armour, reduced_armour, hit_tb, hit_final))
                first = hits[0]
                raw_damage, penetration, armour, armour_after, tb = (
                    first.raw_damage, first.penetration, first.armour_before,
                    first.armour_after_penetration, first.tb_reduction,
                )
                final = sum(hit.final_damage for hit in hits)
                damage_after = damage_before + final
        delta = StateDelta(
            combat_state_before=state_before,
            combat_state_after=state_after,
            action_cost=action_contract.cost.amount,
            reaction_cost=reaction_cost,
            ammo_before=ammo_before,
            ammo_after=(ammo_before - shots_fired if ammo_before is not None else None),
            target_damage_before=damage_before,
            target_damage_after=damage_after,
            weapon_instance_id=weapon.instance_id,
        )
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
            ammo_before - shots_fired if ammo_before is not None else None,
            delta,
            hits=tuple(hits),
            shots_fired=shots_fired,
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


def _resolve_utility_action(
    request: ActionRequest, *, action: str, combat_state: CombatState, pack: Any | None
) -> CombatResult:
    """Build the same delta used by attacks for Aim and Reload."""
    try:
        if request.actor is None:
            raise CombatValidationError("actor is required")
        if pack is None:
            from core.rulepacks import load_rulepack

            pack = load_rulepack(request.actor.system)
        actor_name = _name(request.actor)
        if combat_state.current_actor != actor_name:
            raise CombatValidationError("actor does not have the current turn")  # i18n-exempt: internal validation diagnostic
        contract = _action_contract(pack, action, request.mode)
        actor_state = _combatant(combat_state, actor_name)
        if actor_state.action_budget < contract.cost.amount:
            raise CombatValidationError("insufficient action budget")
        weapon = _find_weapon(request.actor, request)
        catalog = _profiles(request.actor, pack)
        profile = catalog.get(weapon.profile_id)
        if profile is None or profile.kind != "weapon":
            raise CombatValidationError("item is not a weapon profile")  # i18n-exempt: internal validation diagnostic
        ammo_before = ammo_after = None
        if action == "reload":
            if profile.reload != request.mode or profile.clip_size is None:
                raise CombatValidationError("weapon has no supported reload action")  # i18n-exempt: internal validation diagnostic
            if weapon.current_ammo is None:
                raise CombatValidationError("weapon ammo state is missing")
            if weapon.current_ammo >= profile.clip_size:
                raise CombatValidationError("weapon clip is already full")  # i18n-exempt: internal validation diagnostic
            ammo_before, ammo_after = weapon.current_ammo, profile.clip_size
        elif action == "aim" and not profile.is_usable_for_resolution:
            raise CombatValidationError("weapon profile is incomplete")
        before = copy.deepcopy(combat_state)
        after = copy.deepcopy(combat_state)
        after_actor = after.combatants[actor_name]
        after_actor.action_budget -= contract.cost.amount
        if action == "aim":
            after_actor.aim_bonus = contract.attack_modifier
            after_actor.aimed_weapon_instance_id = weapon.instance_id
        else:
            after_actor.aim_bonus = 0
            after_actor.aimed_weapon_instance_id = None
        delta = StateDelta(
            before, after, action_cost=contract.cost.amount,
            ammo_before=ammo_before, ammo_after=ammo_after,
            weapon_instance_id=weapon.instance_id,
        )
        return CombatResult(
            actor_name, _name(request.target), action, weapon.instance_id, profile.id,
            ammo_before=ammo_before, ammo_after=ammo_after, state_delta=delta,
        )
    except CombatValidationError as exc:
        return CombatResult(
            _name(request.actor), _name(request.target), action,
            request.weapon_instance_id, "", validation_failure=str(exc),
        )


def resolve_aim(
    request: ActionRequest, *, combat_state: CombatState, pack: Any | None = None
) -> CombatResult:
    return _resolve_utility_action(request, action="aim", combat_state=combat_state, pack=pack)


def resolve_reload(
    request: ActionRequest, *, combat_state: CombatState, pack: Any | None = None
) -> CombatResult:
    return _resolve_utility_action(request, action="reload", combat_state=combat_state, pack=pack)


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
    if (
        result.actor != _name(request.actor)
        or result.target != _name(request.target)
        or result.weapon_instance_id != request.weapon_instance_id
    ):
        raise CombatValidationError("combat result does not match request")  # i18n-exempt: internal validation diagnostic
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

        pack = load_rulepack(request.actor.system)
    action_contract = _action_contract(pack, result.action, request.mode)
    if delta.action_cost != action_contract.cost.amount:
        raise CombatValidationError("combat action cost delta is invalid")  # i18n-exempt: internal validation diagnostic
    profile = _profiles(request.actor, pack).get(weapon.profile_id)
    if profile is None:
        raise CombatValidationError("weapon profile is missing")
    if result.action == "ranged_attack":
        expected_ammo = delta.ammo_before - result.shots_fired if delta.ammo_before is not None else None
        if result.shots_fired != profile.rate_of_fire.get(request.mode) or delta.ammo_after != expected_ammo:
            raise CombatValidationError("combat ammunition delta is invalid")  # i18n-exempt: internal validation diagnostic
    elif result.action == "reload":
        if delta.ammo_after != profile.clip_size or delta.ammo_before is None:
            raise CombatValidationError("combat reload delta is invalid")  # i18n-exempt: internal validation diagnostic
    elif delta.ammo_before is not None or delta.ammo_after is not None:
        raise CombatValidationError("action has an unexpected ammunition delta")  # i18n-exempt: internal validation diagnostic
    changes_damage = result.action in {"ranged_attack", "melee_attack"}
    if changes_damage and sheet_value(request.target, pack, action_contract.damage_value) != delta.target_damage_before:
        raise CombatValidationError("stale target damage state")
    if changes_damage and delta.target_damage_after is None:
        raise CombatValidationError("combat damage delta is incomplete")  # i18n-exempt: internal validation diagnostic
    if changes_damage and (
        result.final_damage != sum(hit.final_damage for hit in result.hits)
        or delta.target_damage_after != delta.target_damage_before + result.final_damage
    ):
        raise CombatValidationError("combat damage delta is invalid")  # i18n-exempt: internal validation diagnostic
    if not changes_damage and (delta.target_damage_before is not None or delta.target_damage_after is not None):
        raise CombatValidationError("utility action has an unexpected damage delta")  # i18n-exempt: internal validation diagnostic
    damage_key = None
    if changes_damage:
        damage_key = pack.sheet_spec.attr_keys.get(action_contract.damage_value) if pack.sheet_spec else None
        if not damage_key:
            raise CombatValidationError("combat damage value has no writable attribute")  # i18n-exempt: internal validation diagnostic
    attributes_before = copy.deepcopy(request.target.attributes) if changes_damage else None
    weapon_state_before = copy.deepcopy(weapon.state)
    combat_before = copy.deepcopy(combat_state)
    try:
        if delta.ammo_before is not None:
            weapon.state["current_ammo"] = delta.ammo_after
        if changes_damage:
            request.target.attributes[damage_key] = delta.target_damage_after
        _replace_combat_state(combat_state, delta.combat_state_after)
    except Exception:
        if changes_damage:
            request.target.attributes.clear()
            request.target.attributes.update(attributes_before)
        weapon.state.clear()
        weapon.state.update(weapon_state_before)
        _replace_combat_state(combat_state, combat_before)
        raise
