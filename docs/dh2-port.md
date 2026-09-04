# Dark Heresy 2e port notes

The DH2 port is built from the structured Russian rules corpus already stored in the НРИ ВАХА Neon project. The PDF remains the authority for manual verification, but the migration work consumes the normalized `rule_sections` / `rule_chunks` records so we do not parse the same book twice.

## Source corpus

- `rule_sources.source_key`: `DH2_RU_OKP_V1_8`
- title: `Dark Heresy Second Edition — русский перевод ОКП v1.8`
- language: `ru`
- source type: `COMPILED`

## Stage 1 source map

| Loreweaver rulepack path | Neon section | Source meaning |
|---|---|---|
| `alias.WS` | `CH01_H004` | Навык Рукопашной (НР) |
| `alias.BS` | `CH01_H005` | Навык Стрельбы (НС) |
| `alias.S` | `CH01_H006` | Сила (С) |
| `alias.T` | `CH01_H007` | Выносливость (В) |
| `alias.Ag` | `CH01_H008` | Ловкость (Л) |
| `alias.Int` | `CH01_H009` | Интеллект (И) |
| `alias.Per` | `CH01_H010` | Восприятие (Вос) |
| `alias.WP` | `CH01_H011` | Сила Воли (СВ) |
| `alias.Fel` | `CH01_H012` | Общительность (О) |
| `alias.Inf` | `CH01_H013` | Влияние (Вл) |
| `derived.*B` | `CH01_H003` | characteristic bonus = tens digit |
| `resolution.roll/compare` | `CH01_M03`, `CH01_M04` | percentile roll-under core mechanic |
| `resolution.difficulties` | `CH01_H024` | complete +60 ... -60 difficulty ladder |
| situational target handling | `CH01_H018` | sum modifiers into the target |
| `resolution.margin` | `CH01_H022` | positive DoS / negative DoF, starting at one and stepping per full 10 points |
| `initiative.roll` | `CH07_H011` | `1d10 + Agility Bonus` |
| `sheet.Wounds` / `sheet.Damage` | `CH07_H098`, `CH07_H099` | Wounds are a threshold; accrued Damage is tracked separately and can exceed it |
| `derived.FatigueThreshold` | `CH07_H103` | Fatigue Threshold = Toughness Bonus + Willpower Bonus |
| `sheet.Fatigue` | `CH07_M08`, `CH07_H103` | Fatigue is an upward-counting level; exceeding the threshold causes unconsciousness |

## Stage 1 representation notes

Loreweaver already treats `CheckOutcome.margin` as a system-defined signed comparison metric. DH2 uses that field for degrees rather than raw numeric distance:

- `+1`, `+2`, ... = degrees of success;
- `-1`, `-2`, ... = degrees of failure.

This lets generic opposed checks compare native DH2 degree counts without adding system-specific conditionals to core code.

Damage and Fatigue deliberately are **not** declared as Loreweaver `vitals`. A vital is a current pool clamped to a maximum, while DH2 tracks both as counters that grow upward and may legally exceed their thresholds. They are ordinary sheet attributes exposed through resource meters instead:

- Damage meter: `DAMAGE / WOUNDS`;
- Fatigue meter: `FATIGUE / FATIGUE_THRESHOLD`.

The meter is presentational only; crossing a threshold is handled by later deterministic combat/state rules and is never silently clamped by the sheet layer.

## Deliberately not ported yet

Stage 1 does **not** invent values or mechanics for areas whose source slice has not been mapped and tested. In particular:

- Chapter II character generation and home-world characteristic modifiers;
- Fate threshold / Fate Point initialization and spending;
- Insanity and Corruption tracks;
- the full skill list, training levels and characteristic bindings;
- action economy;
- attack modes and rate of fire;
- hit-location digit reversal;
- dodge/parry reactions;
- penetration, armor by location and Toughness reduction;
- weapon qualities, ammunition and reload state;
- righteous fury and critical-effect tables;
- conditions and duration tracking;
- psychic powers.

Those are separate port stages. If a rule cannot be represented by the existing generic rulepack DSL, the port should identify the missing generic primitive instead of adding `if system == "dh2"` logic to the engine.

## Neon content that is not rulepack data

The `GM_RUNTIME_DIRECTIVES_RU` source and `combat_protocols` describe how our AI GM should run a table, not the Dark Heresy rules themselves. They belong in Loreweaver KP skills / `expertise` / `turn_checks` / hooks, not in `dh2.yaml`.
