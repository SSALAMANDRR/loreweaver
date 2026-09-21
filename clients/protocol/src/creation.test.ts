import { describe, expect, test } from "bun:test"
import { isServerFrame } from "./client"
import type { StateFrame } from "./types"

const base: StateFrame = {
  type: "state",
  party: [],
  initiative: [],
  online: 1,
}
describe("generic creation lifecycle wire contract", () => {
  test("accepts presentation metadata for both generic text input forms", () => {
    const input = { label: "Subject", placeholder: "Comets", description: "Name a subject" }
    const state: StateFrame = {
      ...base,
      creation: {
        active: true, complete: false, profile_id: "artisan", stage_index: 0,
        stage_count: 1, completed_stages: [],
        stage: {
          id: "craft", kind: "layer", options: [{
            id: "artisan", label: "Artisan", fixed: true, choices: [
              { id: "subject", label: "Craft", free: true, family: "StarCraft", options: [], input },
              { id: "training", label: "Training", free: false, options: [
                { id: "orbital", label: "Orbital craft", specialization: true, input },
              ] },
            ],
          }],
        },
      },
    }
    expect(isServerFrame(state)).toBe(true)
    const choices = state.creation!.stage!.options![0]!.choices
    expect(choices[0]!.input).toEqual(input)
    expect(choices[1]!.options[0]!.input).toEqual(input)
  })
  test("accepts older state without claiming readiness", () => {
    expect(isServerFrame(base)).toBe(true)
    expect(base.readiness).toBeUndefined()
  })
  test("accepts localized choices without knowing any system", () => {
    const state: StateFrame = {
      ...base,
      readiness: {
        ready: false,
        managed: true,
        phase: "finalization",
        blocked_reference: "",
      },
      finalization: {
        can_roll: false,
        can_resolve: true,
        complete: false,
        expression: "1d100",
        result: {
          roll: 18,
          id: "opaque-row",
          label: "Результат",
          source: "book",
          rules: [],
        },
        choices: [
          {
            id: "opaque-choice",
            label: "Выбор",
            free: false,
            options: [{ id: "a", label: "Вариант" }],
          },
        ],
      },
    }
    expect(isServerFrame(state)).toBe(true)
    expect(
      isServerFrame({
        ...state,
        finalization: { ...state.finalization, choices: [{}] },
      }),
    ).toBe(false)
    expect(isServerFrame({ ...state, readiness: { ready: "yes" } })).toBe(false)
  })
})
