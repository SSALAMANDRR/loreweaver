# Combat action transport

The runtime combat slice uses the existing room lock, character sheets, room
state, and event bus. `action_request` is a generic choice from the
server-authored `state.combat` catalog; no rule name is interpreted by Studio.
The gateway calls the existing combat resolvers, applies their `StateDelta`,
and commits sheet documents plus encounter state and the party resource cache
in one compare-and-swap SQLite transaction. A failed validation or stale write
changes none of those rows.

## Encounter (protocol 2.7)

An encounter is opened only by the keeper (`.combat start`), never lazily by a
first action: initiative must be rolled server-side before anyone acts. The
engine stores the initiative order, current actor, round, per-combatant budgets
and at most one pending reaction in the `combat_state` room row. Every
combatant carries an opaque `controller`: the reserved `keeper` token for NPCs
or the member id owning a player sheet. Authority is checked against it, never
against a client-supplied role.

NPCs reuse existing primitives: an `npc` record whose `stat_char` names a
`sheet` document not owned by a player. That sheet leaves the public party
roster when the encounter starts. No DH2-specific NPC system exists.

Defender reactions are a two-phase attack. The attack commits its costs (action
budget, ammunition, lost aim) and stops before damage with a `pending_reaction`
when the defender can still react; the defender's controller answers with a
declared reaction or `decline` in a separate `action_request`, and only then is
damage rolled and committed. The attacker may not name the reaction. A resolved
pending reaction cannot be answered twice: the pending id is gone and the request
id is recorded for idempotency.

## Secrecy

Combat state never leaves the server raw. `core.combat.project_combat_state`
and `project_combat_result` are the single outbound chokepoint, keyed by the
document layer's `Viewer`: players do not see hidden combatants, keeper-side
counters, NPC skill targets, armour/TB mitigation or damage counters, or the
`combat_state_before/after` snapshots. `action_result` is fanned out with
`RoomHub.publish_each` so each connection gets its own projection; the replay
lane records the player-grade copy.

## Narration

Combat narration is a separate, single-call lane over the committed result. It
has no tools, sheet writes, or Keeper pool access, and its whole input is
player-grade: the player projection of the result plus scene, encounter order
and public NPC descriptions read through player projections. Only a fully
resolved action is narrated; an open reaction window or a turn pass is not. The
structured result remains authoritative if narration fails.

The Keeper's own prompt (`core.prompt_sections` game state) lists the engine
encounter at keeper grade, including hidden combatants and the pending reaction.
The older free-form `initiative_tracker` tool remains separate and does not
drive the engine encounter.
