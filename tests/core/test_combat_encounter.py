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
