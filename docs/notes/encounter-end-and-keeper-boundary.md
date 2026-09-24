# Encounter end and the Keeper's mechanical boundary

## The encounter ends when one side has nobody left

Live play showed a keeper-owned PC alone in an encounter after its only opponent was
defeated, passing turns to itself round after round. The encounter had no notion of
sides, so nothing could tell that the fight was decided.

The source ends an encounter when "the fight is over" (Chapter VII, «Шаг 6: Конец
Столкновения»): steps 4 and 5 repeat until then, and the book gives no mechanical test
for it. "One side has nobody left in the fight" is the engine's reading of that, not a
number from the book. It is the one point past which no combat action is possible.

Every combatant now has a `side`: `party` for a member-controlled character,
`opposition` for a keeper-controlled one, and `party` for a keeper NPC the keeper marks
as an ally (`.combat start <npc>, +<ally>`). An encounter row stored before sides
existed derives the side from the controller (`core.combat.combatant_side`), so an
in-flight fight concludes on its next action without a migration.

`core.combat.encounter_concluded` is true when at most one side still has a combatant
in the fight and no attack waits on a reaction. `gateway.combat_actions._commit`
checks it after every committed action. If it is true, the same compare-and-swap
transaction deletes the encounter row and writes the keeper-grade `combat_aftermath`
row (who fought, on which side, who was taken out). A crash therefore cannot leave a
decided fight open or an ended one without its record. The deciding `action_result`
carries `encounter_ended` and is projected against the state it closed, so the player
projection does not fall back to masking both sides. The room then gets a state frame
without `state.combat` (Studio closes the panel), a system notice, and a narration of
the deciding action told that the scene is over.

A PC whose Damage passes Wounds is not defeated by the engine: that outcome needs the
Critical Effect tables. A fight the party loses therefore still ends with `.combat end`,
which also writes the aftermath.

A defeated NPC keeps its Damage on its sheet. `.combat start` refuses a sheet that a
declared defeat rule already takes out (`core.combat.sheet_is_defeated`), so a fallen
troop cannot silently rejoin the next fight.

## What the Keeper may and may not decide

The same live session sent «контрольный в голову» as free text. The Keeper ran a WS
`skill_check`, then narrated a kill; the combat engine never saw an attack. The
boundary is now:

| Player intent | Lane | Who decides the outcome |
|---|---|---|
| Talk, look, move, describe | narrative (Keeper) | Keeper |
| Non-combat test (Awareness, Security…) | Keeper `skill_check` | dice + pack ladder |
| Attack a combatant still in the fight | combat controls (`action_request`) | combat engine |
| Finish off, search or bind a combatant out of the fight | narrative (Keeper) | Keeper, no roll |

The rules behind the last row come from two places. «Эффектная Гибель» (Chapter XII,
p. 469) says a troop is dead or out of action at the GM's choice. The engine records
only "out of the fight". A coup de grâce on such a target changes nothing mechanical,
so it needs no roll, and no roll may be invented for it.

It is enforced in three places, all generic:

- While an encounter is open, `skill_check` refuses a check whose characteristic is a
  pack combat `attack_value` (`core.combat.attack_values`, read from `combat.yaml`)
  and tells the Keeper to route the player to the combat controls.
- The Keeper's game-state section adds an encounter contract line while a fight is
  open. After it ends, the section lists the aftermath, with each opponent's outcome
  and "no roll needed to finish off, search or bind".
- The combat-narration lane gets `encounter_ended` and is told to close the scene and
  not raise anyone again.

The Keeper still cannot open an encounter from a tool: a fight starts only by the
keeper's `.combat start`. A Keeper tool that opens encounters would be a new
model-driven mutation of combat state and needs its own decision.

Date: 2026-09-24.
