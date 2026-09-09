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
