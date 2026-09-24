"""Encounter lifecycle over the generic combat core, driven by the DH2 pack data.

Rule sources (DH2_RU_OKP_V1_8): initiative CH07_H011; turns/rounds CH07_H002,
CH07_H003, CH07_H008, CH07_H009; action limits CH07_H013, CH07_H014, CH07_H019;
reactions CH07_H015, CH07_H029, CH07_H061; aim CH07_H021.
"""

import copy
import json
from types import SimpleNamespace

import pytest

from core.character_manager import CharacterSheet
from core.combat import (
    DECLINE_REACTION,
    KEEPER_CONTROLLER,
    ActionRequest,
    CombatValidationError,
    EncounterCombatant,
    InitiativeEntry,
    apply_combat_state_delta,
    apply_reaction_delta,
    apply_state_delta,
    order_initiative,
    project_combat_result,
    project_combat_state,
    resolve_aim,
    resolve_end_turn,
    resolve_first_shot,
    resolve_melee_attack,
    resolve_reaction,
    roll_initiative,
    start_encounter,
)
from core.dice_engine import DiceRoller, seed_dice
from core.documents import KEEPER_VIEWER, PLAYER_VIEWER, Viewer
from core.item_model import ItemInstance, load_item_catalog
from core.rulepacks import load_rulepack
from core.sheets import sheet_value


class ScriptedDice:
    """Returns scripted totals in order; fails loudly if the script runs out."""

    def __init__(self, totals):
        self.totals = list(totals)
        self.expressions = []

    def roll_expression(self, expression, is_check=False):
        self.expressions.append(expression)
        return SimpleNamespace(total=self.totals.pop(0))


def _pc(pack, catalog, name="Ada", ag=45):
    sheet = CharacterSheet(name, "dh2")
    sheet.attributes.update({"BS": 50, "WS": 50, "S": 40, "T": 30, "Ag": ag, "Dodge": 1, "Parry": 1, "DAMAGE": 0})
    sheet.equipment = [
        ItemInstance.create(catalog.resolve("lasgun"), state={"current_ammo": 20}),
        ItemInstance.create(catalog.resolve("sword")),
    ]
    return sheet


def _npc(pack, catalog, name="Cultist", ag=30):
    sheet = CharacterSheet(name, "dh2")
    sheet.attributes.update({"BS": 40, "WS": 40, "S": 30, "T": 30, "Ag": ag, "Dodge": 1, "Parry": 1, "DAMAGE": 0})
    sheet.equipment = [
        ItemInstance.create(catalog.resolve("autogun"), state={"current_ammo": 20}),
        ItemInstance.create(catalog.resolve("knife")),
    ]
    return sheet


def _encounter(dice_totals=(9, 3)):
    pack = load_rulepack("dh2")
    catalog = load_item_catalog(pack)
    pc, npc = _pc(pack, catalog), _npc(pack, catalog)
    state, rolls = start_encounter(
        [EncounterCombatant(pc, "u1"), EncounterCombatant(npc, KEEPER_CONTROLLER)],
        pack=pack, dice=ScriptedDice(dice_totals),
    )
    return pack, pc, npc, state, rolls


def _weapon(sheet, profile):
    return next(item for item in sheet.equipment if item.profile_id == profile)


def test_initiative_is_1d10_plus_agility_bonus_from_the_pack():
    pack = load_rulepack("dh2")
    sheet = _pc(pack, load_item_catalog(pack), ag=47)
    assert pack.initiative_roll == "1d10 + {AgB}"
    seed_dice(7)
    entry = roll_initiative(sheet, pack, DiceRoller())
    assert entry.expression == "1d10 + 4"
    assert 1 <= entry.total - sheet_value(sheet, pack, "AgB") <= 10


def test_initiative_orders_high_to_low_then_agility_then_repeated_roll_off():
    pack = load_rulepack("dh2")
    sheets = {name: CharacterSheet(name, "dh2") for name in ("a", "b", "c", "d")}
    for name, ag in {"a": 30, "b": 40, "c": 40, "d": 40}.items():
        sheets[name].attributes["Ag"] = ag
    entries = [InitiativeEntry(name, "", total) for name, total in {"a": 12, "b": 12, "c": 12, "d": 15}.items()]
    # b and c tie on total and Agility: roll-off 5/5 ties again and is repeated, 2/8 separates.
    dice = ScriptedDice([5, 5, 2, 8])
    ordered = order_initiative(entries, sheets, pack, dice)
    assert [entry.name for entry in ordered] == ["d", "c", "b", "a"]
    assert ordered[1].tie_break == (40, 8) and ordered[3].tie_break == (30,)
    assert dice.expressions == ["1d10"] * 4


def test_start_encounter_stores_order_current_actor_round_and_controllers():
    # ScriptedDice yields whole-expression totals; the filled expressions carry each AgB.
    pack, pc, npc, state, rolls = _encounter(dice_totals=(7, 12))
    assert [(entry.name, entry.expression, entry.total) for entry in rolls] == [
        ("Cultist", "1d10 + 3", 12), ("Ada", "1d10 + 4", 7),
    ]
    assert state.order == ["Cultist", "Ada"] and state.current_actor == "Cultist" and state.round_number == 1
    assert state.combatants["Ada"].controller == "u1"
    assert state.combatants["Cultist"].controller == KEEPER_CONTROLLER
    assert state.combatants["Ada"].initiative == 7


def test_end_turn_advances_then_wraps_into_a_new_round_restoring_budgets():
    pack, pc, npc, state, _ = _encounter()
    assert state.order == ["Ada", "Cultist"]
    state.combatants["Cultist"].reactions_remaining = 0
    state.combatants["Ada"].action_budget = 0
    state.combatants["Ada"].turn_actions = ["aim:full"]
    apply_combat_state_delta(state, resolve_end_turn(state, pack=pack))
    assert (state.round_number, state.current_actor) == (1, "Cultist")
    # Reactions are per round (CH07_H015): not restored by a same-round turn change.
    assert state.combatants["Cultist"].reactions_remaining == 0
    assert state.combatants["Cultist"].action_budget == 2
    apply_combat_state_delta(state, resolve_end_turn(state, pack=pack))
    assert (state.round_number, state.current_actor) == (2, "Ada")
    assert state.combatants["Cultist"].reactions_remaining == 1
    assert state.combatants["Ada"].action_budget == 2 and state.combatants["Ada"].turn_actions == []


def test_out_of_turn_action_is_rejected_without_mutation():
    pack, pc, npc, state, _ = _encounter()
    before = copy.deepcopy(state)
    request = ActionRequest(npc, pc, _weapon(npc, "autogun").instance_id, attack_roll=10)
    result = resolve_first_shot(request, combat_state=state, pack=pack, reaction_window=True, pending_id="p")
    assert result.validation_failure == "actor does not have the current turn"
    assert state == before


def test_two_half_actions_must_differ_and_only_one_attack_per_turn():
    pack, pc, npc, state, _ = _encounter()
    lasgun, sword = _weapon(pc, "lasgun"), _weapon(pc, "sword")
    aim = ActionRequest(pc, None, lasgun.instance_id, mode="half")
    apply_state_delta(aim, resolve_aim(aim, combat_state=state, pack=pack), combat_state=state, pack=pack)
    second_aim = resolve_aim(aim, combat_state=state, pack=pack)
    assert second_aim.validation_failure == "action was already taken this turn"

    pack, pc, npc, state, _ = _encounter()
    shot = ActionRequest(pc, npc, _weapon(pc, "lasgun").instance_id, attack_roll=90)
    apply_state_delta(shot, resolve_first_shot(shot, combat_state=state, pack=pack), combat_state=state, pack=pack)
    swing = ActionRequest(pc, npc, _weapon(pc, "sword").instance_id, attack_roll=10)
    rejected = resolve_melee_attack(swing, combat_state=state, pack=pack)
    assert rejected.validation_failure == "attack action limit already taken this turn"
    assert sword is not None


def _pending_shot(pack, pc, npc, state, attack_roll=5):
    request = ActionRequest(pc, npc, _weapon(pc, "lasgun").instance_id, attack_roll=attack_roll)
    result = resolve_first_shot(request, combat_state=state, pack=pack, reaction_window=True, pending_id="p-1")
    apply_state_delta(request, result, combat_state=state, pack=pack)
    return result


def test_a_hit_opens_a_pending_reaction_before_damage_and_spends_the_attack():
    pack, pc, npc, state, _ = _encounter()
    result = _pending_shot(pack, pc, npc, state)
    assert result.pending_reaction["choices"] == ["dodge"]  # ranged: Dodge only (CH07_H029)
    assert result.final_damage == 0 and result.hits == ()
    assert npc.attributes["DAMAGE"] == 0
    assert _weapon(pc, "lasgun").current_ammo == 19
    assert state.pending_reaction["defender"] == "Cultist"
    assert state.combatants["Ada"].action_budget == 1
    with pytest.raises(CombatValidationError, match="reaction is pending"):
        resolve_end_turn(state, pack=pack)
    blocked = resolve_aim(ActionRequest(pc, None, _weapon(pc, "lasgun").instance_id, mode="half"), combat_state=state, pack=pack)
    assert blocked.validation_failure == "a reaction is pending"


def test_melee_hit_offers_parry_and_dodge_and_a_miss_offers_nothing():
    pack, pc, npc, state, _ = _encounter()
    swing = ActionRequest(pc, npc, _weapon(pc, "sword").instance_id, attack_roll=5)
    result = resolve_melee_attack(swing, combat_state=state, pack=pack, reaction_window=True, pending_id="m")
    assert result.pending_reaction["choices"] == ["parry", "dodge"]
    miss = ActionRequest(pc, npc, _weapon(pc, "sword").instance_id, attack_roll=99)
    missed = resolve_melee_attack(miss, combat_state=state, pack=pack, reaction_window=True, pending_id="m")
    assert missed.pending_reaction is None and missed.success is False


def test_attacker_cannot_choose_the_defenders_reaction_in_a_reaction_window():
    pack, pc, npc, state, _ = _encounter()
    request = ActionRequest(pc, npc, _weapon(pc, "lasgun").instance_id, attack_roll=5, reaction_type="dodge")
    result = resolve_first_shot(request, combat_state=state, pack=pack, reaction_window=True, pending_id="p")
    assert result.validation_failure == "the attacker cannot choose the defender's reaction"


@pytest.mark.parametrize(("choice", "roll", "damage"), [("dodge", 10, 0), ("dodge", 95, 6), (DECLINE_REACTION, None, 6)])
def test_defender_reaction_finishes_the_attack_atomically(choice, roll, damage):
    pack, pc, npc, state, _ = _encounter()
    _pending_shot(pack, pc, npc, state)
    result = resolve_reaction(
        pending_id="p-1", choice=choice, attacker=pc, defender=npc, combat_state=state, pack=pack,
        reaction_roll=roll, damage_rolls=(9,),
    )
    assert result.ok, result.validation_failure
    assert result.final_damage == damage
    apply_reaction_delta(npc, result, combat_state=state, pack=pack)
    assert npc.attributes["DAMAGE"] == damage
    assert state.pending_reaction is None
    assert state.combatants["Cultist"].reactions_remaining == (1 if choice == DECLINE_REACTION else 0)
    assert result.reaction["type"] == choice


def test_parry_resolves_through_the_same_reaction_flow():
    pack, pc, npc, state, _ = _encounter()
    swing = ActionRequest(pc, npc, _weapon(pc, "sword").instance_id, attack_roll=5)
    result = resolve_melee_attack(swing, combat_state=state, pack=pack, reaction_window=True, pending_id="m")
    apply_state_delta(swing, result, combat_state=state, pack=pack)
    parried = resolve_reaction(
        pending_id="m", choice="parry", attacker=pc, defender=npc, combat_state=state, pack=pack, reaction_roll=5,
    )
    assert parried.reaction["target"] == sheet_value(npc, pack, "ParryTarget")
    assert parried.reaction["prevents_hit"] is True and parried.final_damage == 0


def test_duplicate_or_unoffered_reactions_are_rejected_without_mutation():
    pack, pc, npc, state, _ = _encounter()
    _pending_shot(pack, pc, npc, state)
    before = copy.deepcopy(state)
    parry = resolve_reaction(pending_id="p-1", choice="parry", attacker=pc, defender=npc, combat_state=state, pack=pack)
    assert parry.validation_failure == "reaction is not offered for this attack"
    wrong_id = resolve_reaction(pending_id="other", choice="dodge", attacker=pc, defender=npc, combat_state=state, pack=pack)
    assert wrong_id.validation_failure == "no matching reaction is pending"
    assert state == before
    first = resolve_reaction(
        pending_id="p-1", choice="dodge", attacker=pc, defender=npc, combat_state=state, pack=pack, reaction_roll=10,
    )
    apply_reaction_delta(npc, first, combat_state=state, pack=pack)
    again = resolve_reaction(
        pending_id="p-1", choice="dodge", attacker=pc, defender=npc, combat_state=state, pack=pack, reaction_roll=10,
    )
    assert again.validation_failure == "no matching reaction is pending"
    with pytest.raises(CombatValidationError):
        apply_reaction_delta(npc, first, combat_state=state, pack=pack)


def test_defender_without_a_reaction_takes_damage_immediately():
    pack, pc, npc, state, _ = _encounter()
    state.combatants["Cultist"].reactions_remaining = 0
    request = ActionRequest(pc, npc, _weapon(pc, "lasgun").instance_id, attack_roll=5, damage_roll=9)
    result = resolve_first_shot(request, combat_state=state, pack=pack, reaction_window=True, pending_id="p")
    assert result.pending_reaction is None and result.final_damage == 6


def test_using_a_reaction_loses_a_prepared_aim():
    pack, pc, npc, state, _ = _encounter()
    state.combatants["Cultist"].aim_bonus = 20
    state.combatants["Cultist"].aimed_weapon_instance_id = _weapon(npc, "autogun").instance_id
    _pending_shot(pack, pc, npc, state)
    result = resolve_reaction(
        pending_id="p-1", choice="dodge", attacker=pc, defender=npc, combat_state=state, pack=pack, reaction_roll=95,
        damage_rolls=(1,),
    )
    apply_reaction_delta(npc, result, combat_state=state, pack=pack)
    assert state.combatants["Cultist"].aim_bonus == 0
    assert state.combatants["Cultist"].aimed_weapon_instance_id is None


def test_npc_attacks_player_on_its_turn_and_the_player_declines():
    pack, pc, npc, state, _ = _encounter()
    apply_combat_state_delta(state, resolve_end_turn(state, pack=pack))
    request = ActionRequest(npc, pc, _weapon(npc, "knife").instance_id, attack_roll=5)
    pending = resolve_melee_attack(request, combat_state=state, pack=pack, reaction_window=True, pending_id="n")
    apply_state_delta(request, pending, combat_state=state, pack=pack)
    assert state.pending_reaction["defender"] == "Ada"
    final = resolve_reaction(
        pending_id="n", choice=DECLINE_REACTION, attacker=npc, defender=pc, combat_state=state, pack=pack,
        damage_rolls=(10,),
    )
    apply_reaction_delta(pc, final, combat_state=state, pack=pack)
    assert pc.attributes["DAMAGE"] == final.final_damage > 0


# --- projection sentinels (iron rule #3) ------------------------------------------------------


def _hidden_scene():
    pack = load_rulepack("dh2")
    catalog = load_item_catalog(pack)
    pc = _pc(pack, catalog)
    npc = _npc(pack, catalog)
    sniper = _npc(pack, catalog, name="SECRET-SNIPER")
    state, _ = start_encounter(
        [
            EncounterCombatant(pc, "u1"),
            EncounterCombatant(npc, KEEPER_CONTROLLER),
            EncounterCombatant(sniper, KEEPER_CONTROLLER, hidden=True),
        ],
        pack=pack, dice=ScriptedDice([9, 3, 1]),
    )
    return pack, pc, npc, sniper, state


def test_player_projection_drops_hidden_combatants_and_keeper_side_counters():
    pack, pc, npc, sniper, state = _hidden_scene()
    state.combatants["Cultist"].aim_bonus = 20
    state.combatants["Cultist"].aimed_weapon_instance_id = "SECRET-WEAPON-ID"
    player = project_combat_state(state, Viewer(role="player", member_id="u1"))
    text = json.dumps(player)
    assert "SECRET-SNIPER" not in text and "SECRET-WEAPON-ID" not in text
    assert set(player["combatants"]) == {"Ada"}
    assert [entry["name"] for entry in player["order"]] == ["Ada", "Cultist"]
    assert player["order"][0]["controlled"] is True and player["order"][1]["controlled"] is False
    keeper = project_combat_state(state, KEEPER_VIEWER)
    assert "SECRET-SNIPER" in json.dumps(keeper) and keeper["combatants"]["Cultist"]["aim_bonus"] == 20


def test_hidden_current_actor_and_hidden_attacker_are_masked_for_players():
    pack, pc, npc, sniper, state = _hidden_scene()
    state.current_actor = "SECRET-SNIPER"
    request = ActionRequest(sniper, pc, _weapon(sniper, "autogun").instance_id, attack_roll=5)
    result = resolve_first_shot(request, combat_state=state, pack=pack, reaction_window=True, pending_id="s")
    apply_state_delta(request, result, combat_state=state, pack=pack)
    player = project_combat_state(state, Viewer(role="player", member_id="u1"))
    assert player["current_actor"] is None
    assert player["pending_reaction"]["attacker"] == ""
    assert player["pending_reaction"]["choices"] == ["dodge"]
    assert "attack_roll" not in player["pending_reaction"]
    bystander = project_combat_state(state, Viewer(role="player", member_id="u2"))
    assert "choices" not in bystander["pending_reaction"]
    from dataclasses import asdict

    public = project_combat_result(asdict(result), state, PLAYER_VIEWER)
    text = json.dumps(public)
    assert "SECRET-SNIPER" not in text and public["attack_target"] is None
    assert "combat_state_before" not in public["state_delta"]


def test_result_projection_hides_npc_mitigation_and_skill_values_from_players():
    pack, pc, npc, state, _ = _encounter()
    request = ActionRequest(pc, npc, _weapon(pc, "lasgun").instance_id, attack_roll=5, damage_roll=9)
    state.combatants["Cultist"].reactions_remaining = 0
    result = resolve_first_shot(request, combat_state=state, pack=pack, reaction_window=True, pending_id="p")
    apply_state_delta(request, result, combat_state=state, pack=pack)
    from dataclasses import asdict

    raw = asdict(result)
    public = project_combat_result(raw, state, PLAYER_VIEWER)
    assert public["final_damage"] == raw["final_damage"]
    assert public["tb_reduction"] is None and public["hits"][0]["armour_before"] is None
    assert public["state_delta"]["target_damage_after"] is None
    assert public["attack_target"] == raw["attack_target"]  # the player's own skill stays visible
    assert project_combat_result(raw, state, KEEPER_VIEWER) == raw


# --- Standard Attack / Aim / tie-break regressions (CH07_H051, CH07_H021, CH07_H011) -----------


def test_standard_attack_is_an_ordinary_plus_ten_test_in_both_modes():
    pack, pc, npc, state, _ = _encounter()
    shot = resolve_first_shot(
        ActionRequest(pc, npc, _weapon(pc, "lasgun").instance_id, attack_roll=60, damage_roll=1),
        combat_state=copy.deepcopy(state), pack=pack,
    )
    assert shot.attack_target == 60 and shot.success  # BS 50 + 10: a roll of 60 now hits
    swing = resolve_melee_attack(
        ActionRequest(pc, npc, _weapon(pc, "sword").instance_id, attack_roll=60, damage_roll=1),
        combat_state=copy.deepcopy(state), pack=pack,
    )
    assert swing.attack_target == 60 and swing.success


def test_the_standard_attack_bonus_does_not_leak_into_burst_modes():
    pack = load_rulepack("dh2")
    catalog = load_item_catalog(pack)
    pc, npc = _pc(pack, catalog), _npc(pack, catalog)
    pc.equipment.append(ItemInstance.create(catalog.resolve("autogun"), state={"current_ammo": 30}))
    state, _ = start_encounter(
        [EncounterCombatant(pc, "u1"), EncounterCombatant(npc, KEEPER_CONTROLLER)], pack=pack, dice=ScriptedDice([9, 3]),
    )
    autogun = _weapon(pc, "autogun").instance_id
    semi = resolve_first_shot(ActionRequest(pc, npc, autogun, mode="semi", attack_roll=99), combat_state=state, pack=pack)
    full = resolve_first_shot(ActionRequest(pc, npc, autogun, mode="full", attack_roll=99), combat_state=state, pack=pack)
    assert (semi.attack_target, full.attack_target) == (50, 40)


@pytest.mark.parametrize(("aim_mode", "expected"), [("half", 70), ("full", 80)])
def test_aim_stacks_with_the_standard_attack_bonus(aim_mode, expected):
    pack, pc, npc, state, _ = _encounter()
    lasgun = _weapon(pc, "lasgun").instance_id
    aim = ActionRequest(pc, None, lasgun, mode=aim_mode)
    apply_state_delta(aim, resolve_aim(aim, combat_state=state, pack=pack), combat_state=state, pack=pack)
    if aim_mode == "full":
        apply_combat_state_delta(state, resolve_end_turn(state, pack=pack))
        apply_combat_state_delta(state, resolve_end_turn(state, pack=pack))
    shot = resolve_first_shot(ActionRequest(pc, npc, lasgun, attack_roll=99), combat_state=state, pack=pack)
    assert shot.attack_target == expected  # BS 50 + Standard 10 + Aim 10/20


def test_a_defender_cannot_react_during_its_own_turn():
    pack, pc, npc, state, _ = _encounter()
    _pending_shot(pack, pc, npc, state)
    state.current_actor = "Cultist"  # a corrupted/raced state must still be refused
    result = resolve_reaction(pending_id="p-1", choice="dodge", attacker=pc, defender=npc, combat_state=state, pack=pack)
    assert result.validation_failure == "a reaction cannot be used during the defender's own turn"


def _tie_scene(monkeypatch, repeat):
    import core.combat as combat

    pack = load_rulepack("dh2")
    data = dict(combat._combat_data(pack))
    data["initiative"] = {"order": "descending", "tie_breakers": [{"value": "Ag"}, {"roll": "1d10", **({"repeat_on_tie": True} if repeat else {})}]}
    monkeypatch.setattr(combat, "_combat_data", lambda _pack: data)
    sheets = {name: CharacterSheet(name, "dh2") for name in ("x", "y")}
    for sheet in sheets.values():
        sheet.attributes["Ag"] = 40
    return pack, sheets, [InitiativeEntry("x", "", 10), InitiativeEntry("y", "", 10)]


def test_repeated_roll_off_is_an_explicit_pack_policy(monkeypatch):
    pack, sheets, entries = _tie_scene(monkeypatch, repeat=True)
    dice = ScriptedDice([4, 4, 3, 7])
    assert [entry.name for entry in order_initiative(entries, sheets, pack, dice)] == ["y", "x"]
    assert dice.totals == []


def test_without_the_policy_a_second_tie_keeps_declaration_order_without_raising(monkeypatch):
    pack, sheets, entries = _tie_scene(monkeypatch, repeat=False)
    dice = ScriptedDice([4, 4])
    assert [entry.name for entry in order_initiative(entries, sheets, pack, dice)] == ["x", "y"]


def test_dh2_marks_the_roll_off_repeat_as_an_implementation_choice_not_canon():
    import core.combat as combat

    data = combat._combat_data(load_rulepack("dh2"))
    assert data["initiative"]["tie_breakers"][-1] == {"roll": "1d10", "repeat_on_tie": True}
    assert "initiative_roll_off_repeat" in data["provenance"]["implementation_choices"]
    assert data["provenance"]["standard_attack_modifier"] == "CH07_H051"


# --- burst action economy (CH07_H033 Table 7-1, CH07_H048, CH07_H035, CH07_H019) -------------


@pytest.mark.parametrize("mode", ["semi", "full"])
def test_a_burst_is_a_half_attack_action_leaving_a_non_attack_half_action(mode):
    from gateway.combat_actions import available_actions

    pack = load_rulepack("dh2")
    catalog = load_item_catalog(pack)
    pc, npc = _pc(pack, catalog), _npc(pack, catalog)
    pc.equipment.append(ItemInstance.create(catalog.resolve("autogun"), state={"current_ammo": 30}))
    state, _ = start_encounter(
        [EncounterCombatant(pc, "u1"), EncounterCombatant(npc, KEEPER_CONTROLLER)], pack=pack, dice=ScriptedDice([9, 3]),
    )
    autogun = _weapon(pc, "autogun").instance_id
    npc.attributes["Dodge"] = 0
    state.combatants["Cultist"].reactions_remaining = 0
    burst = ActionRequest(pc, npc, autogun, mode=mode, attack_roll=99)
    result = resolve_first_shot(burst, combat_state=state, pack=pack)
    assert result.ok and result.state_delta.action_cost == 1
    apply_state_delta(burst, result, combat_state=state, pack=pack)
    assert state.combatants["Ada"].action_budget == 1
    offered = {action["id"] for action in available_actions(pack, pc, ["Cultist"], "en", state)}
    # One Attack action per turn: no second attack of any kind, but Aim (Concentration) remains.
    assert "ranged_attack" not in offered and "melee_attack" not in offered
    assert "aim" in offered


def test_aim_then_burst_in_one_turn_and_aim_after_an_attack_carries_to_the_next_attack():
    pack = load_rulepack("dh2")
    catalog = load_item_catalog(pack)
    pc, npc = _pc(pack, catalog), _npc(pack, catalog)
    pc.equipment.append(ItemInstance.create(catalog.resolve("autogun"), state={"current_ammo": 30}))
    state, _ = start_encounter(
        [EncounterCombatant(pc, "u1"), EncounterCombatant(npc, KEEPER_CONTROLLER)], pack=pack, dice=ScriptedDice([9, 3]),
    )
    state.combatants["Cultist"].reactions_remaining = 0
    autogun = _weapon(pc, "autogun").instance_id
    aim = ActionRequest(pc, None, autogun, mode="half")
    apply_state_delta(aim, resolve_aim(aim, combat_state=state, pack=pack), combat_state=state, pack=pack)
    burst = resolve_first_shot(ActionRequest(pc, npc, autogun, mode="semi", attack_roll=99), combat_state=state, pack=pack)
    assert burst.ok and burst.attack_target == 50 + 10  # BS 50, Short Burst +0, half Aim +10

    # Attack first, then Aim with the other half action: the bonus waits for the next attack.
    pack, pc2 = pack, _pc(pack, catalog, name="Bo")
    state2, _ = start_encounter(
        [EncounterCombatant(pc2, "u2"), EncounterCombatant(npc, KEEPER_CONTROLLER)], pack=pack, dice=ScriptedDice([9, 3]),
    )
    state2.combatants["Cultist"].reactions_remaining = 0
    lasgun = _weapon(pc2, "lasgun").instance_id
    shot = ActionRequest(pc2, npc, lasgun, attack_roll=99)
    apply_state_delta(shot, resolve_first_shot(shot, combat_state=state2, pack=pack), combat_state=state2, pack=pack)
    aim2 = ActionRequest(pc2, None, lasgun, mode="half")
    apply_state_delta(aim2, resolve_aim(aim2, combat_state=state2, pack=pack), combat_state=state2, pack=pack)
    apply_combat_state_delta(state2, resolve_end_turn(state2, pack=pack))
    apply_combat_state_delta(state2, resolve_end_turn(state2, pack=pack))
    next_shot = resolve_first_shot(ActionRequest(pc2, npc, lasgun, attack_roll=99), combat_state=state2, pack=pack)
    assert next_shot.attack_target == 50 + 10 + 10  # Standard +10, carried half Aim +10


# --- canonical attack details (Глава VII с. 278-284; CH07_H043, CH07_H059, CH07_H064) --------


def test_short_range_is_strictly_below_half_the_weapon_range():
    pack, pc, npc, state, _ = _encounter()
    lasgun = _weapon(pc, "lasgun").instance_id  # range 100 m
    exactly_half = resolve_first_shot(ActionRequest(pc, npc, lasgun, attack_roll=99, distance=50), combat_state=state, pack=pack)
    under_half = resolve_first_shot(ActionRequest(pc, npc, lasgun, attack_roll=99, distance=49), combat_state=state, pack=pack)
    assert (exactly_half.attack_target, under_half.attack_target) == (60, 70)


def test_total_situational_modifier_is_capped_at_the_pack_limit(monkeypatch):
    import core.combat as combat

    pack, pc, npc, state, _ = _encounter()
    data = dict(combat._combat_data(pack))
    data["modifier_limit"] = 25
    monkeypatch.setattr(combat, "_combat_data", lambda _pack: data)
    shot = resolve_first_shot(ActionRequest(pc, npc, _weapon(pc, "lasgun").instance_id, attack_roll=99, distance=1), combat_state=state, pack=pack)
    assert shot.attack_target == 50 + 25  # +10 Standard +30 point blank, capped at +25


@pytest.mark.parametrize(
    ("first_roll", "expected"),
    [
        # hits 1..6 on one target; the 6th uses the table's "further hits" column
        (1, ["head", "head", "right_arm", "body", "right_arm", "body"]),          # 01 -> 10 head
        (51, ["right_arm", "right_arm", "body", "head", "body", "right_arm"]),    # 51 -> 15 right arm
        (72, ["left_arm", "left_arm", "body", "head", "body", "left_arm"]),      # 72 -> 27 left arm
        (4, ["body", "body", "right_arm", "head", "right_arm", "body"]),          # 04 -> 40 body
        (97, ["right_leg", "right_leg", "body", "right_arm", "head", "body"]),    # 97 -> 79 right leg
    ],
)
def test_additional_hits_follow_table_7_2(first_roll, expected):
    from core.combat import _additional_hit_location, _location, _location_roll

    pack = load_rulepack("dh2")
    first = _location(pack, _location_roll(pack, first_roll))
    assert [_additional_hit_location(pack, first, index) for index in range(6)] == expected
    assert _additional_hit_location(pack, first, 11) == expected[-1]


def test_a_burst_spreads_hits_by_table_7_2_not_on_one_location():
    pack = load_rulepack("dh2")
    catalog = load_item_catalog(pack)
    pc, npc = _pc(pack, catalog), _npc(pack, catalog)
    pc.attributes["BS"] = 100
    pc.equipment.append(ItemInstance.create(catalog.resolve("autogun"), state={"current_ammo": 30}))
    state, _ = start_encounter([EncounterCombatant(pc, "u1"), EncounterCombatant(npc, KEEPER_CONTROLLER)], pack=pack, dice=ScriptedDice([9, 3]))
    state.combatants["Cultist"].reactions_remaining = 0
    burst = resolve_first_shot(
        ActionRequest(pc, npc, _weapon(pc, "autogun").instance_id, mode="full", attack_roll=4),
        combat_state=state, pack=pack, dice=DiceRoller(),
    )
    assert len(burst.hits) >= 5  # 04 vs 90: nine degrees, one hit per degree
    assert [hit.location for hit in burst.hits][:6] == ["body", "body", "right_arm", "head", "right_arm", "body"]
    assert sum(hit.degrees_substituted for hit in burst.hits) <= 1


def test_one_damage_die_takes_the_degrees_of_success_when_that_is_higher():
    pack, pc, npc, state, _ = _encounter()
    state.combatants["Cultist"].reactions_remaining = 0
    pc.attributes["BS"] = 100
    # Roll 5 vs 110: 11 degrees. Supplied lasgun totals 4 and (for the second hit) none.
    shot = resolve_first_shot(ActionRequest(pc, npc, _weapon(pc, "lasgun").instance_id, attack_roll=5, damage_roll=4), combat_state=state, pack=pack)
    assert shot.hits[0].raw_damage == 4 - 1 + 11 and shot.hits[0].degrees_substituted
    low = resolve_first_shot(ActionRequest(pc, npc, _weapon(pc, "lasgun").instance_id, attack_roll=99, damage_roll=13), combat_state=state, pack=pack)
    assert low.hits[0].raw_damage == 13 and not low.hits[0].degrees_substituted  # 2 degrees < die 10
