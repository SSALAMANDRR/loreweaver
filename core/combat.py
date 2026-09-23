"""Small deterministic, pack-driven combat substrate.

The engine owns validation, resolution, and atomic state application. The
rulepack owns action costs, reaction allowances, reset semantics, hit locations,
and the other system-specific values consumed here.
"""
from __future__ import annotations

import copy
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from core.check_outcome import CheckOutcome, RollDetail
from core.dice_engine import DiceRoller
from core.documents import Viewer
from core.item_model import ItemInstance, ItemProfileCatalog
from core.sheets import sheet_value
from core.yaml_safety import safe_load_no_aliases

# The room_state row holding the serialized encounter (written by gateway.combat_actions).
COMBAT_STATE_KEY = "combat_state"
# The opaque controller token for keeper-run combatants; any other non-empty
# controller is the member id that owns the combatant's sheet.
KEEPER_CONTROLLER = "keeper"
DECLINE_REACTION = "decline"
# Engine lifecycle action ids; a rulepack may not declare actions with these names.
END_TURN_ACTION = "end_turn"
REACTION_ACTION = "reaction"
_RESERVED_ACTIONS = frozenset({END_TURN_ACTION, REACTION_ACTION})
_ROLL_OFF_LIMIT = 100


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
    turn_actions: list[str] = field(default_factory=list)
    initiative: int | None = None
    controller: str = ""
    hidden: bool = False
    # Out of the fight by a pack-declared defeat rule: no further turns, not a target.
    defeated: bool = False


@dataclass
class CombatState:
    """Encounter state: initiative order, turn pointer, budgets, one pending reaction."""

    round_number: int
    current_actor: str
    combatants: dict[str, CombatantState] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)
    pending_reaction: dict[str, Any] | None = None


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
    subtypes: tuple[str, ...] = ()


@dataclass(frozen=True)
class _CombatContract:
    action_budget_per_turn: int
    reactions_per_round: int
    reaction_reset: str
    distinct_actions_per_turn: bool = False
    subtype_limits: Mapping[str, int] = field(default_factory=dict)
    reaction_window: str | None = None
    aim_lost_on_reaction: bool = False


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
    mode: str = ""
    # Set when the attack hit and now waits for the defender's reaction choice.
    pending_reaction: Mapping[str, Any] | None = None
    # The committed damage took the target out of the fight (pack defeat rule).
    target_defeated: bool = False

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


def combat_state_from_json(raw: str) -> CombatState:
    """Parse a stored encounter row; transport bookkeeping keys are ignored."""
    data = json.loads(raw)
    return CombatState(
        round_number=data["round_number"],
        current_actor=data["current_actor"],
        combatants={name: CombatantState(**row) for name, row in data["combatants"].items()},
        order=list(data.get("order") or []),
        pending_reaction=data.get("pending_reaction"),
    )


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
    window = raw.get("reaction_window")
    if window is not None and window != "before_damage":
        raise CombatValidationError("unsupported reaction window semantics")
    limits_raw = raw.get("subtype_limits_per_turn") or {}
    if not isinstance(limits_raw, Mapping):
        raise CombatValidationError("turn subtype limits must be a mapping")  # i18n-exempt: internal validation diagnostic
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
        distinct_actions_per_turn=raw.get("distinct_actions_per_turn") is True,
        subtype_limits={
            str(name): _required_int(limit, where="subtype limit", minimum=1)
            for name, limit in limits_raw.items()
        },
        reaction_window=window,
        aim_lost_on_reaction=raw.get("aim_lost_on_reaction") is True,
    )


@dataclass(frozen=True)
class _DefeatRule:
    field: str
    equals: str


@dataclass(frozen=True)
class _DefeatContract:
    damage_value: str
    wounds_value: str
    rules: tuple[_DefeatRule, ...]


def _defeat_contract(pack: Any) -> _DefeatContract | None:
    """Pack-declared defeat rules; a pack that declares none never defeats anyone."""
    raw = _combat_data(pack).get("defeat")
    if raw is None:
        return None
    if not isinstance(raw, Mapping) or not raw.get("damage_value") or not raw.get("wounds_value"):
        raise CombatValidationError("combat defeat contract is incomplete")  # i18n-exempt: internal validation diagnostic
    rules: list[_DefeatRule] = []
    for rule in raw.get("rules") or []:
        if (
            not isinstance(rule, Mapping)
            or rule.get("when") != "critical_damage"
            or rule.get("status") != "defeated"
            or not isinstance(rule.get("field"), str)
            or not isinstance(rule.get("equals"), str)
        ):
            raise CombatValidationError("unsupported combat defeat rule")  # i18n-exempt: internal validation diagnostic
        rules.append(_DefeatRule(rule["field"], rule["equals"]))
    return _DefeatContract(str(raw["damage_value"]), str(raw["wounds_value"]), tuple(rules))


def _sheet_field(sheet: Any, pack: Any, name: str) -> Any:
    keys = getattr(pack.sheet_spec, "field_keys", {}) if pack.sheet_spec is not None else {}
    return getattr(sheet, str(keys.get(name, name)), None)


def _defeated_by(pack: Any, target: Any, damage_before: int, damage_after: int) -> bool:
    """Whether this action dealt Critical Damage (damage received while above Wounds)
    to a target that a declared rule takes out of the fight at that point."""
    contract = _defeat_contract(pack)
    if contract is None or damage_after <= damage_before:
        return False
    if damage_after <= sheet_value(target, pack, contract.wounds_value):
        return False
    return any(_sheet_field(target, pack, rule.field) == rule.equals for rule in contract.rules)


def _action_contract(pack: Any, action: str, mode: str) -> _ActionContract:
    if action in _RESERVED_ACTIONS:
        raise CombatValidationError(f"combat action {action!r} is reserved by the engine")
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
    subtypes_raw = mode_raw.get("subtypes") or []
    if not isinstance(subtypes_raw, list) or not all(isinstance(value, str) and value for value in subtypes_raw):
        raise CombatValidationError("action subtypes must be a list of names")  # i18n-exempt: internal validation diagnostic
    extra_values["subtypes"] = tuple(subtypes_raw)
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
    next_state.turn_actions = []
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


@dataclass(frozen=True)
class InitiativeEntry:
    """One combatant's committed initiative roll and the tie-break values used."""

    name: str
    expression: str
    total: int
    tie_break: tuple[int, ...] = ()


@dataclass(frozen=True)
class EncounterCombatant:
    sheet: Any
    controller: str
    hidden: bool = False


def _initiative_contract(pack: Any) -> list[Mapping[str, Any]]:
    raw = _combat_data(pack).get("initiative")
    if not isinstance(raw, Mapping) or raw.get("order") != "descending":
        raise CombatValidationError("combat initiative contract is missing")  # i18n-exempt: internal validation diagnostic
    breakers = raw.get("tie_breakers") or []
    if not isinstance(breakers, list):
        raise CombatValidationError("initiative tie breakers must be a list")  # i18n-exempt: internal validation diagnostic
    for breaker in breakers:
        valid = isinstance(breaker, Mapping) and (
            set(breaker) == {"value"} or set(breaker) in ({"roll"}, {"roll", "repeat_on_tie"})
        )
        if not valid or ("repeat_on_tie" in breaker and not isinstance(breaker["repeat_on_tie"], bool)):
            raise CombatValidationError("unsupported initiative tie breaker")  # i18n-exempt: internal validation diagnostic
    return list(breakers)


def roll_initiative(sheet: Any, pack: Any, dice: DiceRoller | None = None) -> InitiativeEntry:
    """Roll the pack's ``initiative.roll``; ``{Name}`` slots read canonical sheet values."""
    expression = str(getattr(pack, "initiative_roll", "") or "")
    if not expression:
        raise CombatValidationError("rulepack declares no initiative roll")  # i18n-exempt: internal validation diagnostic
    filled = re.sub(r"\{([^{}]+)\}", lambda match: str(sheet_value(sheet, pack, match.group(1))), expression)
    total = (dice or DiceRoller()).roll_expression(_dice_expression(filled)).total
    return InitiativeEntry(_name(sheet), filled, int(total))


def order_initiative(
    entries: list[InitiativeEntry], sheets: Mapping[str, Any], pack: Any, dice: DiceRoller | None = None
) -> list[InitiativeEntry]:
    """Order highest first, applying the pack's tie breakers only inside tied groups."""
    breakers = _initiative_contract(pack)
    roller = dice or DiceRoller()

    def settle(group: list[InitiativeEntry], depth: int) -> list[InitiativeEntry]:
        # A tie no declared breaker separates keeps the combatants' declaration order.
        if len(group) < 2 or depth >= len(breakers):
            return group
        breaker = breakers[depth]
        if "value" in breaker:
            keyed = [
                (sheet_value(sheets[entry.name], pack, str(breaker["value"])), entry) for entry in group
            ]
            return split(keyed, lambda tied: settle(tied, depth + 1))
        roll = _dice_expression(str(breaker["roll"]))
        if breaker.get("repeat_on_tie") is not True:
            keyed = [(roller.roll_expression(roll).total, entry) for entry in group]
            return split(keyed, lambda tied: settle(tied, depth + 1))
        # Pack policy: repeat the roll-off among whoever it leaves tied.
        for _attempt in range(_ROLL_OFF_LIMIT):
            keyed = [(roller.roll_expression(roll).total, entry) for entry in group]
            if len({value for value, _entry in keyed}) > 1:
                return split(keyed, lambda tied: settle(tied, depth))
        return group

    def split(keyed: list[tuple[int, InitiativeEntry]], resolve_tie: Any) -> list[InitiativeEntry]:
        ordered: list[InitiativeEntry] = []
        for value in sorted({value for value, _entry in keyed}, reverse=True):
            tied = [
                InitiativeEntry(entry.name, entry.expression, entry.total, (*entry.tie_break, int(value)))
                for key, entry in keyed
                if key == value
            ]
            ordered.extend(resolve_tie(tied) if len(tied) > 1 else tied)
        return ordered

    by_total: dict[int, list[InitiativeEntry]] = {}
    for entry in entries:
        by_total.setdefault(entry.total, []).append(entry)
    ordered: list[InitiativeEntry] = []
    for total in sorted(by_total, reverse=True):
        ordered.extend(settle(by_total[total], 0))
    return ordered


def start_encounter(
    combatants: Iterable[EncounterCombatant], *, pack: Any, dice: DiceRoller | None = None
) -> tuple[CombatState, list[InitiativeEntry]]:
    """Roll initiative for every combatant and open round one on the highest roll."""
    members = list(combatants)
    names = [_name(member.sheet) for member in members]
    if len(members) < 2 or len(set(names)) != len(names) or not all(names):
        raise CombatValidationError("an encounter needs at least two uniquely named combatants")  # i18n-exempt: internal validation diagnostic
    if any(getattr(member.sheet, "system", "") != pack.system for member in members):
        raise CombatValidationError("every combatant must use the encounter rulepack")  # i18n-exempt: internal validation diagnostic
    if any(not member.controller for member in members):
        raise CombatValidationError("every combatant needs a controller")  # i18n-exempt: internal validation diagnostic
    roller = dice or DiceRoller()
    sheets = {_name(member.sheet): member.sheet for member in members}
    rolled = [roll_initiative(member.sheet, pack, roller) for member in members]
    ordered = order_initiative(rolled, sheets, pack, roller)
    order = [entry.name for entry in ordered]
    state = create_combat_state(order, current_actor=order[0], pack=pack)
    state.order = order
    for member, entry in zip(members, rolled, strict=True):
        combatant = state.combatants[entry.name]
        combatant.initiative = entry.total
        combatant.controller = member.controller
        combatant.hidden = member.hidden
    return state, ordered


def resolve_end_turn(state: CombatState, *, pack: Any) -> StateDelta:
    """Advance to the next combatant still in the fight, opening a new round on wrap.

    Defeated combatants are skipped. When only the current combatant remains, its
    next turn is the next round's.
    """
    if state.pending_reaction is not None:
        raise CombatValidationError("a reaction is pending")
    if not state.order or state.current_actor not in state.order:
        raise CombatValidationError("encounter has no initiative order")  # i18n-exempt: internal validation diagnostic
    index = state.order.index(state.current_actor)
    size = len(state.order)
    for step in range(1, size + 1):
        position = index + step
        candidate = state.order[position % size]
        if state.combatants[candidate].defeated:
            continue
        return resolve_turn_transition(
            state,
            next_actor=candidate,
            pack=pack,
            round_number=state.round_number + 1 if position >= size else state.round_number,
        )
    raise CombatValidationError("no combatant remains in the fight")  # i18n-exempt: internal validation diagnostic


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


def _turn_entry(action: str, mode: str) -> str:
    return f"{action}:{mode}"


def _check_turn_constraints(
    pack: Any, action: str, action_contract: _ActionContract, state: CombatState, actor_state: CombatantState
) -> None:
    """Pack-declared per-turn limits: repeated actions and per-subtype caps."""
    if state.pending_reaction is not None:
        raise CombatValidationError("a reaction is pending")
    contract = _contract(pack)
    taken = [entry.split(":", 1) for entry in actor_state.turn_actions]
    if contract.distinct_actions_per_turn and any(done == action for done, _mode in taken):
        raise CombatValidationError("action was already taken this turn")  # i18n-exempt: internal validation diagnostic
    for subtype in action_contract.subtypes:
        limit = contract.subtype_limits.get(subtype)
        if limit is None:
            continue
        used = sum(
            1 for done, done_mode in taken if subtype in _action_contract(pack, done, done_mode).subtypes
        )
        if used >= limit:
            raise CombatValidationError(f"{subtype} action limit already taken this turn")  # i18n-exempt: internal validation diagnostic


def _reaction_choices(
    action_contract: _ActionContract, state: CombatState, defender: str
) -> list[str]:
    """Reactions the defender may still choose: sheet-backed, affordable, outside own turn."""
    if defender == state.current_actor:
        return []
    defender_state = _combatant(state, defender)
    return [
        kind
        for kind, reaction in action_contract.reactions.items()
        if reaction.target_value is not None and defender_state.reactions_remaining >= reaction.cost
    ]


def _damage_hits(
    *,
    pack: Any,
    catalog: ItemProfileCatalog,
    profile: Any,
    action_contract: _ActionContract,
    attacker: Any,
    target: Any,
    hit_count: int,
    location: str | None,
    roller: DiceRoller,
    damage_rolls: tuple[int, ...] = (),
    location_rolls: tuple[int, ...] = (),
    damage_roll: int | None = None,
) -> list[HitResult]:
    if profile.damage_expression is None or profile.penetration is None:
        raise CombatValidationError("weapon damage profile is incomplete")
    if damage_rolls and len(damage_rolls) != hit_count:
        raise CombatValidationError("damage rolls do not match hit count")  # i18n-exempt: internal validation diagnostic
    if location_rolls and len(location_rolls) != hit_count:
        raise CombatValidationError("location rolls do not match hit count")  # i18n-exempt: internal validation diagnostic
    hits: list[HitResult] = []
    for index in range(hit_count):
        hit_location = _location(pack, location_rolls[index]) if location_rolls else location
        hit_damage = (
            damage_rolls[index] if damage_rolls else
            damage_roll if index == 0 and damage_roll is not None else
            roller.roll_expression(_dice_expression(profile.damage_expression)).total
        )
        if isinstance(hit_damage, bool) or not isinstance(hit_damage, int) or hit_damage < 0:
            raise CombatValidationError("damage roll must be non-negative")  # i18n-exempt: internal validation diagnostic
        if action_contract.damage_bonus_value is not None:
            hit_damage += sheet_value(attacker, pack, action_contract.damage_bonus_value)
        hit_armour = 0
        for item in getattr(target, "equipment", []) or []:
            if isinstance(item, ItemInstance):
                armour_profile = catalog.get(item.profile_id)
                if armour_profile is not None and armour_profile.kind == "armour":
                    hit_armour = max(hit_armour, armour_profile.armor_at(hit_location))
        hit_penetration = int(profile.penetration)
        reduced_armour = max(0, hit_armour - hit_penetration)
        hit_tb = sheet_value(target, pack, action_contract.toughness_value)
        hit_final = max(0, int(hit_damage) - reduced_armour - hit_tb)
        hits.append(HitResult(hit_location, hit_damage, hit_penetration, hit_armour, reduced_armour, hit_tb, hit_final))
    return hits


def _lose_aim(combatant: CombatantState) -> None:
    combatant.aim_bonus = 0
    combatant.aimed_weapon_instance_id = None


def _resolve_attack(
    request: ActionRequest,
    *,
    action: str,
    combat_state: CombatState,
    pack: Any | None = None,
    dice: DiceRoller | None = None,
    reaction_window: bool = False,
    pending_id: str | None = None,
) -> CombatResult:
    """Resolve one pack-declared attack without mutating entity or combat state.

    With ``reaction_window`` a hit stops before damage when the defender still has a
    reaction to choose; the result then carries ``pending_reaction`` instead of damage.
    """
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
        if actor_name == target_name:
            raise CombatValidationError("target is the acting combatant")  # i18n-exempt: internal validation diagnostic
        if combat_state.current_actor != actor_name:
            raise CombatValidationError("actor does not have the current turn")  # i18n-exempt: internal validation diagnostic
        action_contract = _action_contract(pack, action, request.mode)
        actor_state = _combatant(combat_state, actor_name)
        if _combatant(combat_state, target_name).defeated:
            raise CombatValidationError("target is out of the fight")  # i18n-exempt: internal validation diagnostic
        if actor_state.action_budget < action_contract.cost.amount:
            raise CombatValidationError("insufficient action budget")
        _check_turn_constraints(pack, action, action_contract, combat_state, actor_state)
        if reaction_window and (request.reaction_type is not None or request.reaction_roll is not None):
            raise CombatValidationError("the attacker cannot choose the defender's reaction")  # i18n-exempt: internal validation diagnostic
        if reaction_window and not pending_id:
            raise CombatValidationError("a reaction window needs a pending id")  # i18n-exempt: internal validation diagnostic
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
        state_after.combatants[actor_name].turn_actions.append(_turn_entry(action, request.mode))
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
            choices = (
                _reaction_choices(action_contract, combat_state, target_name)
                if reaction_window and _contract(pack).reaction_window == "before_damage"
                else []
            )
            if choices:
                pending = {
                    "id": pending_id,
                    "attacker": actor_name,
                    "defender": target_name,
                    "action": action,
                    "mode": request.mode,
                    "weapon_instance_id": weapon.instance_id,
                    "weapon_profile_id": profile.id,
                    "attack_target": target_value,
                    "attack_roll": attack.total,
                    "margin": outcome.margin,
                    "degrees": degrees,
                    "location": location,
                    "hit_count": hit_count,
                    "shots_fired": shots_fired,
                    "ammo_before": ammo_before,
                    "ammo_after": ammo_before - shots_fired if ammo_before is not None else None,
                    "choices": choices,
                }
                state_after.pending_reaction = pending
                delta = StateDelta(
                    combat_state_before=state_before,
                    combat_state_after=state_after,
                    action_cost=action_contract.cost.amount,
                    ammo_before=ammo_before,
                    ammo_after=pending["ammo_after"],
                    target_damage_before=damage_before,
                    target_damage_after=damage_before,
                    weapon_instance_id=weapon.instance_id,
                )
                return CombatResult(
                    actor_name, target_name, action, weapon.instance_id, profile.id,
                    target_value, attack.total, True, outcome.margin, degrees, location,
                    ammo_before=ammo_before, ammo_after=pending["ammo_after"], state_delta=delta,
                    shots_fired=shots_fired, mode=request.mode, pending_reaction=pending,
                )
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
                if _contract(pack).aim_lost_on_reaction:
                    _lose_aim(target_state)
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
                hits = _damage_hits(
                    pack=pack, catalog=catalog, profile=profile, action_contract=action_contract,
                    attacker=request.actor, target=request.target, hit_count=hit_count,
                    location=location, roller=roller, damage_rolls=request.damage_rolls,
                    location_rolls=request.location_rolls, damage_roll=request.damage_roll,
                )
                first = hits[0]
                raw_damage, penetration, armour, armour_after, tb = (
                    first.raw_damage, first.penetration, first.armour_before,
                    first.armour_after_penetration, first.tb_reduction,
                )
                final = sum(hit.final_damage for hit in hits)
                damage_after = damage_before + final
        target_defeated = _defeated_by(pack, request.target, damage_before, damage_after)
        if target_defeated:
            state_after.combatants[target_name].defeated = True
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
            mode=request.mode,
            target_defeated=target_defeated,
        )
    except CombatValidationError as exc:
        return CombatResult(
            _name(request.actor),
            _name(request.target),
            action,
            request.weapon_instance_id,
            "",
            validation_failure=str(exc),
            mode=request.mode,
        )


def resolve_first_shot(
    request: ActionRequest,
    *,
    combat_state: CombatState,
    pack: Any | None = None,
    dice: DiceRoller | None = None,
    reaction_window: bool = False,
    pending_id: str | None = None,
) -> CombatResult:
    """Resolve one pack-declared single shot without mutating entity or combat state."""
    return _resolve_attack(
        request,
        action="ranged_attack",
        combat_state=combat_state,
        pack=pack,
        dice=dice,
        reaction_window=reaction_window,
        pending_id=pending_id,
    )


def resolve_melee_attack(
    request: ActionRequest,
    *,
    combat_state: CombatState,
    pack: Any | None = None,
    dice: DiceRoller | None = None,
    reaction_window: bool = False,
    pending_id: str | None = None,
) -> CombatResult:
    """Resolve one pack-declared basic melee attack without mutating state."""
    return _resolve_attack(
        request,
        action="melee_attack",
        combat_state=combat_state,
        pack=pack,
        dice=dice,
        reaction_window=reaction_window,
        pending_id=pending_id,
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
        _check_turn_constraints(pack, action, contract, combat_state, actor_state)
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
        after_actor.turn_actions.append(_turn_entry(action, request.mode))
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
            ammo_before=ammo_before, ammo_after=ammo_after, state_delta=delta, mode=request.mode,
        )
    except CombatValidationError as exc:
        return CombatResult(
            _name(request.actor), _name(request.target), action,
            request.weapon_instance_id, "", validation_failure=str(exc), mode=request.mode,
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
    target.order = list(source.order)
    target.pending_reaction = copy.deepcopy(source.pending_reaction)


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


def resolve_reaction(
    *,
    pending_id: str,
    choice: str,
    attacker: Any,
    defender: Any,
    combat_state: CombatState,
    pack: Any,
    dice: DiceRoller | None = None,
    reaction_roll: int | None = None,
    damage_rolls: tuple[int, ...] = (),
    location_rolls: tuple[int, ...] = (),
) -> CombatResult:
    """Finish a pending attack with the defender's choice (a declared reaction or decline)."""
    pending = combat_state.pending_reaction
    try:
        if pending is None or pending.get("id") != pending_id:
            raise CombatValidationError("no matching reaction is pending")
        if attacker is None or defender is None:
            raise CombatValidationError("attacker and defender are required")  # i18n-exempt: internal validation diagnostic
        attacker_name, defender_name = _name(attacker), _name(defender)
        if pending["attacker"] != attacker_name or pending["defender"] != defender_name:
            raise CombatValidationError("reaction does not match the pending attack")  # i18n-exempt: internal validation diagnostic
        if defender_name == combat_state.current_actor:
            raise CombatValidationError("a reaction cannot be used during the defender's own turn")  # i18n-exempt: internal validation diagnostic
        action, mode = str(pending["action"]), str(pending["mode"])
        action_contract = _action_contract(pack, action, mode)
        weapon = _find_weapon(attacker, ActionRequest(attacker, defender, str(pending["weapon_instance_id"])))
        catalog = _profiles(attacker, pack)
        profile = catalog.get(weapon.profile_id)
        if profile is None or profile.id != pending["weapon_profile_id"]:
            raise CombatValidationError("stale weapon state")
        if pack.resolver is None:
            raise CombatValidationError("rulepack has no check resolver")
        state_before = copy.deepcopy(combat_state)
        state_after = copy.deepcopy(combat_state)
        defender_state = _combatant(state_after, defender_name)
        hit_count = int(pending["hit_count"])
        reaction_cost = 0
        roller = dice or DiceRoller()
        if choice == DECLINE_REACTION:
            reaction: dict[str, Any] = {
                "type": DECLINE_REACTION, "declined": True, "spent": False,
                "success": False, "prevents_hit": False,
            }
        else:
            reaction_contract = action_contract.reactions.get(choice)
            if choice not in pending.get("choices", []) or reaction_contract is None or reaction_contract.target_value is None:
                raise CombatValidationError("reaction is not offered for this attack")  # i18n-exempt: internal validation diagnostic
            if defender_state.reactions_remaining < reaction_contract.cost:
                raise CombatValidationError("reaction is unavailable")
            rr = reaction_roll if reaction_roll is not None else roller.roll_detail("1d100").total
            reaction_target = sheet_value(defender, pack, reaction_contract.target_value)
            reaction_outcome = pack.resolver.interpret(_roll_detail(rr), reaction_target)
            reaction_success = bool(reaction_outcome.rank.success)
            reaction_cost = reaction_contract.cost
            defender_state.reactions_remaining -= reaction_cost
            if _contract(pack).aim_lost_on_reaction:
                _lose_aim(defender_state)
            reaction = {
                "type": choice, "roll": rr, "target": reaction_target,
                "success": reaction_success, "prevents_hit": False, "spent": True,
            }
            if reaction_success:
                avoided = hit_count
                if reaction_contract.hits_avoided_per_degree is not None:
                    reaction_degrees = abs(reaction_outcome.margin) if reaction_outcome.margin is not None else 1
                    avoided = min(hit_count, reaction_degrees * reaction_contract.hits_avoided_per_degree)
                    reaction["hits_avoided"] = avoided
                hit_count -= avoided
                reaction["prevents_hit"] = hit_count == 0
        state_after.pending_reaction = None
        damage_before = sheet_value(defender, pack, action_contract.damage_value)
        hits = _damage_hits(
            pack=pack, catalog=catalog, profile=profile, action_contract=action_contract,
            attacker=attacker, target=defender, hit_count=hit_count, location=pending.get("location"),
            roller=roller, damage_rolls=damage_rolls, location_rolls=location_rolls,
        ) if hit_count else []
        final = sum(hit.final_damage for hit in hits)
        target_defeated = _defeated_by(pack, defender, damage_before, damage_before + final)
        if target_defeated:
            defender_state.defeated = True
        delta = StateDelta(
            combat_state_before=state_before,
            combat_state_after=state_after,
            reaction_cost=reaction_cost,
            target_damage_before=damage_before,
            target_damage_after=damage_before + final,
        )
        first = hits[0] if hits else None
        return CombatResult(
            attacker_name, defender_name, action, weapon.instance_id, profile.id,
            pending.get("attack_target"), pending.get("attack_roll"), hit_count > 0,
            pending.get("margin"), pending.get("degrees"), pending.get("location"), reaction,
            first.raw_damage if first else None,
            first.penetration if first else None,
            first.armour_before if first else None,
            first.armour_after_penetration if first else None,
            first.tb_reduction if first else None,
            final,
            pending.get("ammo_before"), pending.get("ammo_after"), delta,
            hits=tuple(hits), shots_fired=int(pending.get("shots_fired") or 0), mode=mode,
            target_defeated=target_defeated,
        )
    except CombatValidationError as exc:
        pending = pending or {}
        return CombatResult(
            _name(attacker), _name(defender), str(pending.get("action") or REACTION_ACTION),
            str(pending.get("weapon_instance_id") or ""), "", validation_failure=str(exc),
            mode=str(pending.get("mode") or ""),
        )


def apply_reaction_delta(defender: Any, result: CombatResult, *, combat_state: CombatState, pack: Any) -> None:
    """Apply a finished reaction's defender damage and combat state together, or neither."""
    if not result.ok or result.state_delta is None:
        raise CombatValidationError("cannot apply invalid combat result")
    delta = result.state_delta
    pending = combat_state.pending_reaction
    if pending is None or result.target != _name(defender) or result.target != pending.get("defender"):
        raise CombatValidationError("stale combat state")
    if delta.action_cost or delta.ammo_before is not None or delta.ammo_after is not None or delta.weapon_instance_id:
        raise CombatValidationError("reaction delta contains attacker changes")  # i18n-exempt: internal validation diagnostic
    if combat_state != delta.combat_state_before:
        raise CombatValidationError("stale combat state")
    action_contract = _action_contract(pack, result.action, result.mode)
    if sheet_value(defender, pack, action_contract.damage_value) != delta.target_damage_before:
        raise CombatValidationError("stale target damage state")
    if (
        delta.target_damage_before is None
        or result.final_damage != sum(hit.final_damage for hit in result.hits)
        or delta.target_damage_after != delta.target_damage_before + result.final_damage
    ):
        raise CombatValidationError("combat damage delta is invalid")  # i18n-exempt: internal validation diagnostic
    damage_key = pack.sheet_spec.attr_keys.get(action_contract.damage_value) if pack.sheet_spec else None
    if not damage_key:
        raise CombatValidationError("combat damage value has no writable attribute")  # i18n-exempt: internal validation diagnostic
    attributes_before = copy.deepcopy(defender.attributes)
    combat_before = copy.deepcopy(combat_state)
    try:
        defender.attributes[damage_key] = delta.target_damage_after
        _replace_combat_state(combat_state, delta.combat_state_after)
    except Exception:
        defender.attributes.clear()
        defender.attributes.update(attributes_before)
        _replace_combat_state(combat_state, combat_before)
        raise


# ---------------------------------------------------------------------------
# Viewer projection: the single outbound chokepoint for combat state and results.
# ---------------------------------------------------------------------------

_PUBLIC_COUNTERS = ("action_budget", "action_budget_max", "reactions_remaining", "reactions_max", "aim_bonus")


def viewer_controls(combatant: CombatantState, viewer: Viewer) -> bool:
    """A keeper runs keeper-controlled combatants; a member runs the combatants they own."""
    if combatant.controller == KEEPER_CONTROLLER:
        return viewer.is_keeper
    return bool(viewer.member_id) and combatant.controller == viewer.member_id


def _keeper_side(combatant: CombatantState | None) -> bool:
    return combatant is not None and combatant.controller == KEEPER_CONTROLLER


def _project_pending(
    pending: Mapping[str, Any] | None, state: CombatState, viewer: Viewer
) -> dict[str, Any] | None:
    if pending is None:
        return None
    if viewer.is_keeper:
        return copy.deepcopy(dict(pending))
    attacker = state.combatants.get(str(pending.get("attacker")))
    defender = state.combatants.get(str(pending.get("defender")))
    if defender is None or defender.hidden:
        return None
    view: dict[str, Any] = {
        "id": pending.get("id"),
        "attacker": "" if attacker is None or attacker.hidden else pending.get("attacker"),
        "defender": pending.get("defender"),
        "action": pending.get("action"),
        "mode": pending.get("mode"),
        "hit_count": pending.get("hit_count"),
    }
    if not _keeper_side(attacker):
        view.update(attack_roll=pending.get("attack_roll"), degrees=pending.get("degrees"))
    if viewer_controls(defender, viewer):
        view["choices"] = list(pending.get("choices") or [])
    return view


def project_combat_state(state: CombatState, viewer: Viewer) -> dict[str, Any]:
    """Keeper sees everything; players never see hidden combatants or keeper-side counters."""
    visible = {
        name: combatant for name, combatant in state.combatants.items()
        if viewer.is_keeper or not combatant.hidden
    }
    order: list[dict[str, Any]] = []
    for name in state.order or list(state.combatants):
        combatant = visible.get(name)
        if combatant is None:
            continue
        entry: dict[str, Any] = {
            "name": name,
            "initiative": combatant.initiative,
            "current": name == state.current_actor,
            "controlled": viewer_controls(combatant, viewer),
            "keeper_controlled": combatant.controller == KEEPER_CONTROLLER,
            "defeated": combatant.defeated,
        }
        if viewer.is_keeper:
            entry["hidden"] = combatant.hidden
        order.append(entry)
    combatants: dict[str, Any] = {}
    for name, combatant in visible.items():
        if viewer.is_keeper:
            combatants[name] = asdict(combatant)
        elif not _keeper_side(combatant):
            combatants[name] = {key: getattr(combatant, key) for key in _PUBLIC_COUNTERS}
    return {
        "round_number": state.round_number,
        "current_actor": state.current_actor if state.current_actor in visible else None,
        "order": order,
        "combatants": combatants,
        "pending_reaction": _project_pending(state.pending_reaction, state, viewer),
    }


def project_combat_result(result: Mapping[str, Any], state: CombatState, viewer: Viewer) -> dict[str, Any]:
    """The per-viewer copy of a committed ``CombatResult`` (``asdict`` form)."""
    view = copy.deepcopy(dict(result))
    if viewer.is_keeper:
        return view
    actor = state.combatants.get(str(view.get("actor")))
    target = state.combatants.get(str(view.get("target")))
    delta = view.get("state_delta")
    if isinstance(delta, dict):
        delta.pop("combat_state_before", None)
        delta.pop("combat_state_after", None)
    if _keeper_side(actor):
        view.update(attack_target=None, ammo_before=None, ammo_after=None, weapon_instance_id="")
        if isinstance(delta, dict):
            delta.update(ammo_before=None, ammo_after=None, weapon_instance_id=None)
    if _keeper_side(target):
        view.update(armour_before=None, armour_after_penetration=None, tb_reduction=None)
        for hit in view.get("hits") or []:
            hit.update(armour_before=None, armour_after_penetration=None, tb_reduction=None)
        if isinstance(delta, dict):
            delta.update(target_damage_before=None, target_damage_after=None)
        if isinstance(view.get("reaction"), dict):
            view["reaction"].pop("target", None)
    if actor is not None and actor.hidden:
        view["actor"] = ""
    if target is not None and target.hidden:
        view["target"] = ""
    if view.get("pending_reaction") is not None:
        view["pending_reaction"] = _project_pending(view["pending_reaction"], state, viewer)
    return view
