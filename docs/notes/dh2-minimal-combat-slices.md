# DH2 minimal combat slices

The initial DH2 combat implementation extends one generic, pack-driven combat
pipeline rather than introducing separate ranged and melee engines. Action cost,
attack and defensive check values, optional damage bonus, eligible weapon
profiles, ammunition consumption, reaction type and hit-location data are all
declared by the rulepack combat sidecar.

The core resolver validates the complete operation, creates a `StateDelta`, and
does not mutate combatants, equipment or damage until `apply_state_delta` checks
the expected-before state and applies the delta atomically. Ranged attacks may
consume an item resource; melee attacks use the same path with no resource
mutation. Both attacks and reactions are graded by the rulepack's existing
`CheckResolver`.

The Combat MVP extension uses the same pipeline for pack-declared half/full
action costs, automatic hit location, Dodge, Aim, Reload, single/semi/full fire,
and a tuple of independently mitigated hits. One aggregate `StateDelta` applies
all hits and ammunition together; individual `HitResult` records expose every
impact without allowing a partial commit.

The local checkout has representative equipment profiles and the DH2 check
resolver, but no authoritative local combat-rule text for the added Aim,
range, fire-mode, and burst-location numbers. Those values in `combat.yaml`
are therefore a local MVP contract requiring source verification before they
are called canonically complete. In particular, additional burst hits currently
reuse the first hit location unless deterministic test inputs provide individual
locations. The laspistol profile remains incomplete and is rejected by the
attack validator. Reload refills a clip without tracking reserve ammunition;
inventory logistics are a separate dependency. Sword's Balanced quality is
represented in item data but has no runtime Parry bonus in this slice.
