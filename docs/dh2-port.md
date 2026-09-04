# Dark Heresy 2e port notes

The DH2 port is built from the structured Russian rules corpus already stored in the НРИ ВАХА Neon project. The PDF remains the authority for manual verification. Chapter II is currently missing from the Neon import, so character-creation rules are read directly from the same Russian `DH_II_-OKP_-_v_1_8.pdf` source and mapped here explicitly rather than guessed.

## Source corpus

- `rule_sources.source_key`: `DH2_RU_OKP_V1_8`
- title: `Dark Heresy Second Edition — русский перевод ОКП v1.8`
- language: `ru`
- source type: `COMPILED`
- Drive file id: `13-NT_gI_Ebad0ju2YZUpnHSYiF8vvSGm`

## Stage 1 source map

| Loreweaver rulepack path | Source | Source meaning |
|---|---|---|
| `alias.WS` | Neon `CH01_H004` | Навык Рукопашной (НР) |
| `alias.BS` | Neon `CH01_H005` | Навык Стрельбы (НС) |
| `alias.S` | Neon `CH01_H006` | Сила (С) |
| `alias.T` | Neon `CH01_H007` | Выносливость (В) |
| `alias.Ag` | Neon `CH01_H008` | Ловкость (Л) |
| `alias.Int` | Neon `CH01_H009` | Интеллект (И) |
| `alias.Per` | Neon `CH01_H010` | Восприятие (Вос) |
| `alias.WP` | Neon `CH01_H011` | Сила Воли (СВ) |
| `alias.Fel` | Neon `CH01_H012` | Общительность (О) |
| `alias.Inf` | Neon `CH01_H013` | Влияние (Вл) |
| `derived.*B` | Neon `CH01_H003` | characteristic bonus = tens digit |
| `resolution.roll/compare` | Neon `CH01_M03`, `CH01_M04` | percentile roll-under core mechanic |
| `resolution.difficulties` | Neon `CH01_H024` | complete +60 ... -60 difficulty ladder |
| situational target handling | Neon `CH01_H018` | sum modifiers into the target |
| `resolution.margin` | Neon `CH01_H022` | positive DoS / negative DoF, starting at one and stepping per full 10 points |
| `initiative.roll` | Neon `CH07_H011` | `1d10 + Agility Bonus` |
| `sheet.Wounds` / `sheet.Damage` | Neon `CH07_H098`, `CH07_H099` | Wounds are a threshold; accrued Damage is tracked separately and can exceed it |
| `derived.FatigueThreshold` | Neon `CH07_H103` | Fatigue Threshold = Toughness Bonus + Willpower Bonus |
| `sheet.Fatigue` | Neon `CH07_M08`, `CH07_H103` | Fatigue is an upward-counting level; exceeding the threshold causes unconsciousness |
| `sheet.skills`, `derived.*Target`, `sheet.check_values` | Neon `CH03_H001`, `CH03_H014`, `CH03_H015` | regular-skill training levels 0–4 and their -20/+0/+10/+20/+30 modifiers |
| regular skill base-characteristic bindings | Neon `CH03_H037` | the 21 non-special skills and their normal governing characteristics |
| `sheet.skill_families` | Neon `CH03_H001`, `CH03_H003`, `CH03_H015`, `CH03_H037` | Special skills require training, every specialization is a separate skill, and trained levels use the same +0/+10/+20/+30 ladder |
| `creation_constraints.attributes[*].roll` | PDF Chapter II, character generation | ordinary characteristic = `2d10+20` |
| `creation_constraints.profiles.*.attribute_rolls` | PDF Chapter II, home-world rules | `+` characteristic = `3d10kh2+20`; `-` characteristic = `3d10kl2+20` |
| `creation_constraints.profiles.*.attributes.FateThreshold` | PDF pp. 36–46 | starting Fate Threshold by home world |
| `creation_constraints.profiles.*.bonus_rolls.emperors_blessing` | PDF Chapter II, home-world rules | roll `1d10`; meeting the listed Emperor's Blessing threshold increases Fate Threshold by 1 |
| `sheet.vitals.FATE` / Fate resource | PDF Chapter II + Chapter VIII Fate rules | current Fate starts full at Fate Threshold and may later be spent separately |

## Stage 1 representation notes

Loreweaver already treats `CheckOutcome.margin` as a system-defined signed comparison metric. DH2 uses that field for degrees rather than raw numeric distance:

- `+1`, `+2`, ... = degrees of success;
- `-1`, `-2`, ... = degrees of failure.

This lets generic opposed checks compare native DH2 degree counts without adding system-specific conditionals to core code.

Damage and Fatigue deliberately are **not** declared as Loreweaver `vitals`. A vital is a current pool clamped to a maximum, while DH2 tracks both as counters that grow upward and may legally exceed their thresholds. They are ordinary sheet attributes exposed through resource meters instead:

- Damage meter: `DAMAGE / WOUNDS`;
- Fatigue meter: `FATIGUE / FATIGUE_THRESHOLD`.

Fate is the opposite shape and therefore **does** fit the generic vital abstraction: current Fate is a spendable pool capped by Fate Threshold. Character creation initializes `FATE` to the final `FATE_THRESHOLD` after Emperor's Blessing is resolved.

### Regular skills

The 21 non-special skills from table 3-3 fit the existing generic sheet substrate. Each `sheet.skills` value stores the **training level**, not the final percentile target:

- `0` = untrained, `-20`;
- `1` = Знает, `+0`;
- `2` = Обучен, `+10`;
- `3` = Опытен, `+20`;
- `4` = Ветеран, `+30`.

A derived `<Skill>Target` combines that modifier with the skill's normal governing characteristic. `sheet.check_values` then redirects a check on the skill to that derived target. This preserves advancement as explicit rank data while using Loreweaver's existing check path unchanged.

`CH03_H016` allows the GM to substitute an alternative characteristic when circumstances justify it. That situational choice is **not** baked into the normal target formula; it needs a generic per-check characteristic override later.

### Special skill families

Seven table-3-3 families are marked Special: Запретные Знания, Лингвистика, Навигация, Общие Знания, Ремесло, Управление and Учёные Знания. `CH03_H001` forbids using a Special skill without training, and `CH03_H003` says every specialization is acquired and improved separately.

Loreweaver now has a generic `sheet.skill_families` primitive for this class of rules. A family declares its base characteristic, aliases, rank-to-target modifiers, and whether an untrained specialization is forbidden or receives a declared modifier.

A surface form such as `Навигация (Варп)` is normalized to a separate canonical storage key such as `Navigation::варп`. `Навигация (Наземная)` is a different key with a different rank. The family name alone remains non-rollable, which prevents the accidental shared-score implementation that would violate DH2.

### Generic creation profiles

`core.creation_profiles` is a system-neutral creation layer. It reads `creation_constraints.profiles` and knows nothing about Dark Heresy or home worlds. A profile may:

- override selected attribute dice expressions;
- set initial sheet attributes;
- set declared sheet meta fields;
- perform named bonus rolls;
- apply attribute additions/replacements when a safe `condexpr` condition succeeds.

DH2 uses this generic primitive for its six core home worlds:

| Profile | + characteristics | - characteristic | Fate | Emperor's Blessing |
|---|---|---|---:|---:|
| Feral World | S, T | Inf | 2 | 3+ |
| Forge World | Int, T | Fel | 3 | 8+ |
| Highborn | Fel, Inf | T | 4 | 10+ |
| Hive World | Ag, Per | WP | 2 | 6+ |
| Shrine World | Fel, WP | Per | 3 | 6+ |
| Voidborn | Int, WP | S | 3 | 5+ |

The book also grants the player one optional reroll of a generated characteristic. That is intentionally **not** auto-consumed by the profile generator because it is a player choice about which result to replace. The later player-owned manual-roll flow should expose that choice explicitly instead of having the engine quietly optimize a PC.

The rulebook also provides an alternative point-buy method: characteristics start at 25 with 60 points to distribute, no characteristic may exceed 40, and home-world `+/-` characteristics change those starts to 30/20. That method is sourced but not yet wired into the profile generator in this slice.

## Deliberately not ported yet

Stage 1 does **not** invent values or mechanics for areas whose source slice has not been mapped and tested. In particular:

- point-buy character generation and the player's one characteristic reroll;
- home-world Wounds, aptitude, talent/bonus and recommended-background payloads;
- backgrounds, roles, aptitudes, XP prices and starting equipment;
- Fate spending/burning/recovery semantics beyond the current/threshold sheet representation;
- Insanity and Corruption tracks;
- situational alternative-characteristic selection for skill checks;
- action economy;
- attack modes and rate of fire;
- hit-location digit reversal;
- dodge/parry reaction state;
- penetration, armor by location and Toughness reduction;
- weapon qualities, ammunition and reload state;
- righteous fury and critical-effect tables;
- conditions and duration tracking;
- psychic powers.

Those are separate port stages. If a rule cannot be represented by the existing generic rulepack DSL, the port should identify the missing generic primitive instead of adding `if system == "dh2"` logic to the engine.

## Neon content that is not rulepack data

The `GM_RUNTIME_DIRECTIVES_RU` source and `combat_protocols` describe how our AI GM should run a table, not the Dark Heresy rules themselves. They belong in Loreweaver KP skills / `expertise` / `turn_checks` / hooks, not in `dh2.yaml`.
