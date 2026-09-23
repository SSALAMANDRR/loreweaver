import { expect, test } from "bun:test"
import { isServerFrame } from "./client"
import type { ActionRequestFrame } from "./types"

test("generic combat request round-trips without client rule names", () => {
  const request: ActionRequestFrame = {
    type: "action_request", id: "r1", actor: "A", target: "B", action: "custom_action",
    mode: "variant", weapon_instance_id: "instance-1",
  }
  expect(JSON.parse(JSON.stringify(request))).toEqual(request)
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
      state_delta: { ammo_before: 4, ammo_after: 3 },
    },
  }
  expect(isServerFrame(JSON.parse(JSON.stringify(result)))).toBe(true)
  expect(isServerFrame({ ...result, result: null })).toBe(false)
  expect(isServerFrame({ ...result, labels: {} })).toBe(false)
})
