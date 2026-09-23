# NPC profiles and defeat

## Opponent profiles are pack data, NPCs are ordinary sheets

A published opponent stat block becomes `rulepacks/data/<system>/npc_profiles.yaml`
data. `core.npc_profiles` materializes it into a plain `CharacterSheet` with
`ItemInstance`s from the pack's item catalog, and `agent.npc.create_npc_from_profile`
writes it as a `sheet` document owned by `npc:<record id>` next to an `npc` record
whose `stat_char` names it. The keeper reaches this through `.npc create <profile> |
<name>` (the existing keeper-only, private cast command). The sheet never enters
the party roster, and the encounter already treats it as a keeper-controlled
combatant. There is no NPC-specific combat model.

The loader fails closed on fidelity. A weapon row is built from the catalog only
if the profile's stated row matches the fields the resolver consumes (damage dice,
penetration, range, rate of fire, clip, reload), and a stated `+N БС` must equal
the materialized Strength Bonus. The first Chapter XII candidates showed why: the
Инфектор Штамма's autogun is printed `O/3/–`, while the catalog autogun
(`CH05_H078`) fires full auto; building it from the catalog would have given the
NPC a weapon mode its profile does not have. Profiles with Unnatural
Characteristics were left out because the sheet cannot yet represent the bonus
they change.

Uniform flak armour appears only in the profiles' hit-location boxes (4 on every
location), not in Table 5-11. It is one catalog row, `npc_profile_flak_armour`,
whose `source_reference` cites those profile pages instead of Chapter V.

## Defeat is a pack-declared rule evaluated in the same delta

`combat.yaml` `defeat:` names the Damage and Wounds values and lists rules of the
form "on Critical Damage, a sheet whose field equals X is defeated". The resolver
evaluates them where it computes the damage, so the `defeated` flag sits in the
same `StateDelta` as the Damage change and commits or fails with it. Only damage
actually received in this action can trigger a rule. DH2 declares one rule: Troop
NPCs, «Эффектная Гибель». The engine marks them *defeated* (out of the fight). It
does not mark them *dead*, because the source leaves "killed or taken out of
action" to the GM.

A defeated combatant is skipped by `resolve_end_turn`, is not offered as a target,
and is refused as one. Nothing else leaves the encounter: PCs and Elite/Master
NPCs past their Wounds keep acting until the Critical Effect tables exist, and the
encounter does not end itself, because combatants have controllers but no sides.
