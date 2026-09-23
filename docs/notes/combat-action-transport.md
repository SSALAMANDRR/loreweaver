# Combat action transport

The first runtime combat slice uses the existing room lock, character sheets,
room state, and event bus. `action_request` is a generic choice from the
server-authored `state.combat` catalog; no rule name is interpreted by Studio.
The gateway calls the existing combat resolvers, applies their `StateDelta`,
and commits both sheet documents plus combat state and the party resource
cache in one SQLite transaction. A failed validation or stale write changes
none of those rows.

Combat narration is a separate, single-call lane over the committed
`CombatResult`. It has no tools, sheet writes, or Keeper pool access. The
structured result remains authoritative if narration fails. The result and
prose share the existing room event/replay bus.

The current turn state starts lazily on the first valid action. The gateway
does not yet advance initiative or collect independent reaction decisions
from defending players. Those require their own interaction design before
a normal multi-round combat session is possible.
