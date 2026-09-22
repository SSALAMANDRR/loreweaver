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

This note covers only First Shot and the sword/knife basic-melee vertical slice.
It deliberately does not establish contracts for multi-hit attacks, weapon
qualities, special actions, criticals, cover, suppression or reload/aim.
