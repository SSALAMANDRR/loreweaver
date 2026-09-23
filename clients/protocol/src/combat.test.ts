import { expect, test } from "bun:test"
import { isServerFrame } from "./client"
import type { ActionRequestFrame, StateFrame } from "./types"

test("generic combat request round-trips without client rule names", () => {
  const request: ActionRequestFrame = {
    type: "action_request", id: "r1", actor: "A", target: "B", action: "custom_action",
    mode: "variant", weapon_instance_id: "instance-1",
  }
  expect(JSON.parse(JSON.stringify(request))).toEqual(request)
  const reaction: ActionRequestFrame = {
    type: "action_request", id: "r2", actor: "B", action: "reaction", mode: "server-choice", pending_id: "p1",
  }
  expect(JSON.parse(JSON.stringify(reaction))).toEqual(reaction)
})

test("structured result validates and malformed payload is rejected", () => {
  const result = {
    type: "action_result", id: "r1", ok: true, validation_failure: null,
    result: {
      actor: "A", target: "B", action: "custom_action", weapon_instance_id: "instance-1",
      weapon_profile_id: "profile", attack_target: 50, attack_roll: 20, success: true,
      margin: 3, degrees: 3, hit_location: "body", reaction: null, raw_damage: 10,
      penetration: 0, armour_before: 3, armour_after_penetration: 3, tb_reduction: 4,
      final_damage: 3, ammo_before: 4, ammo_after: 3, shots_fired: 1,
      hits: [{ location: "body", final_damage: 3 }], validation_failure: null,
      state_delta: { ammo_before: 4, ammo_after: 3 }, mode: "variant", pending_reaction: null,
    },
  }
  expect(isServerFrame(JSON.parse(JSON.stringify(result)))).toBe(true)
  expect(isServerFrame({ ...result, result: null })).toBe(false)
  expect(isServerFrame({ ...result, labels: {} })).toBe(false)
})

const encounterState = (combat: unknown) => ({ type: "state", party: [], initiative: [], online: 1, combat })

test("encounter state with a reaction offer validates; a malformed offer is rejected", () => {
  const combat: StateFrame["combat"] = {
    actor: "B",
    actions: [],
    state: {
      round_number: 2,
      current_actor: "A",
      order: [
        { name: "A", initiative: 12, current: true, controlled: false, keeper_controlled: true },
        { name: "B", initiative: 7, current: false, controlled: true, keeper_controlled: false },
      ],
      combatants: { B: { action_budget: 2 } },
      pending_reaction: { id: "p1", attacker: "A", defender: "B", action: "x", mode: "y", hit_count: 1, choices: ["z"] },
    },
    reaction: {
      id: "p1", actor: "B", attacker: "A", action: "Server attack", hit_count: 1,
      choices: [{ id: "z", label: "Server reaction" }, { id: "decline", label: "No reaction" }],
    },
  }
  expect(isServerFrame(encounterState(combat))).toBe(true)
  expect(isServerFrame(encounterState({ ...combat, reaction: { id: "p1", actor: "B", choices: [{ id: 1 }] } }))).toBe(false)
  expect(isServerFrame(encounterState({ ...combat, state: { round_number: "2" } }))).toBe(false)
  expect(isServerFrame(encounterState({ ...combat, end_turn: { id: "end_turn", label: "End turn" } }))).toBe(true)
})

test("v2.8 manual-dice payload round-trips and surfaces carrying dice specs validate", () => {
  const request: ActionRequestFrame = {
    type: "action_request", id: "m1", actor: "A", target: "B", action: "custom_action", mode: "variant",
    weapon_instance_id: "instance-1", roll_source: "manual", manual_rolls: { attack: [57] },
  }
  expect(JSON.parse(JSON.stringify(request))).toEqual(request)
  const spec = { id: "attack", label: "Attack roll", expression: "1d100", count: 1, sides: 100 }
  const combat = {
    actor: "A", state: null,
    actions: [{
      id: "custom_action", label: "Server attack", targets: ["B"],
      modes: [{ id: "variant", label: "V", weapons: [{ id: "w", label: "W" }], reactions: [], manual_rolls: [spec] }],
    }],
  }
  expect(isServerFrame(encounterState(combat))).toBe(true)
  expect(isServerFrame({ ...encounterState(combat), roll_mode: "manual" })).toBe(true)
})
