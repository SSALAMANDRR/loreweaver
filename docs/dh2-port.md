# Dark Heresy 2e port notes

The DH2 port is built from the structured Russian rules corpus already stored in the НРИ ВАХА Neon project. The PDF remains the authority for manual verification. Chapter II is currently missing from the Neon import, so character-creation rules are read directly from the same Russian `DH_II_-OKP_-_v_1_8.pdf` source and mapped here explicitly rather than guessed.

## Source corpus

- `rule_sources.source_key`: `DH2_RU_OKP_V1_8`
- title: `Dark Heresy Second Edition — русский перевод ОКП v1.8`
- language: `ru`
- source type: `COMPILED`
- Drive file id: `13-NT_gI_Ebad0ju2YZUpnHSYiF8vvSGm`

## Stage 1 source map

The source map below records the initial port. The current implementation has
also added the creation and advancement stages described under **Current status**;
the initial slice is not the full feature inventory.

| Loreweaver path | Source | Source meaning |
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
| `rulepacks/data/dh2/creation.yaml -> layers.background` | PDF pp. 48–62 | all seven core backgrounds, starting rank-1 skills, talents/trait labels, equipment choices, background abilities and one background aptitude choice |
| `core.creation_layers` | engine-generic | explicit multi-stage creation choices; missing choices fail instead of being guessed |

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

The book also grants the player one optional reroll of a generated characteristic.
`creation_flow.yaml` now exposes it as the explicit `profile_reroll` stage. The
player chooses a characteristic or skips the reroll; the second result is final.
The profile generator never silently consumes or optimizes this choice.

The rulebook also provides an alternative point-buy method: characteristics start at 25 with 60 points to distribute, no characteristic may exceed 40, and home-world `+/-` characteristics change those starts to 30/20. That method is sourced but not yet wired into the profile generator in this slice.

### Generic layered creation and DH2 backgrounds

Home world is only the first stage of DH2 creation. `core.creation_layers` adds a second system-neutral substrate for choices that are applied **after** the initial profile without multiplying profiles into every possible combination. Layer data lives beside the pack under `rulepacks/data/<system>/creation.yaml`; core resolves the current pack's sidecar by convention and has no Dark Heresy branches.

A layer option can set declared sheet fields, append list fields, assign ordinary or specialized skill ranks, add equipment, and declare explicit nested player choices. A choice may be a finite option or a free specialization of a declared skill family. If any required choice is missing, application fails before mutating the character.

DH2 currently declares all seven core backgrounds from pp. 48–62:

- Адептус Администратум;
- Адептус Арбитрес;
- Адептус Астра Телепатика;
- Адептус Механикус;
- Адептус Министорум;
- Астра Милитарум;
- Изгой.

Starting skills are written at rank 1 (`Знает`), including separate Special-skill specializations such as `CommonLore::адептус механикус` or `Navigation::наземная`. Talents, traits and background abilities are currently stored as structured sheet lists using the exact Russian source labels; their mechanical effects will be connected when the generic talent/trait/availability/combat primitives are ported. Starting equipment is likewise preserved as source-labelled inventory entries until the Chapter V item catalogue is normalized.

The important agency rule is already executable: `Коммерция или Медика`, `Нападение или Защита`, free `Учёные Знания (выберите одно)`, Mechanicus `Бдительность или Управление (выберите одно)`, equipment alternatives, and background aptitude choices all require explicit player selections. The engine does not optimize a PC behind the player's back.

## Current status

Verified against the source tree on 2026-09-09:

- Percentile checks, difficulty modifiers, signed degrees, initiative, characteristic
  bonuses, regular skill ranks and independent Special-skill specializations are
  declared in `dh2.yaml` and exercised by `tests/core/test_dh2_rulepack.py`.
- `creation_flow.yaml` orders characteristics, the optional reroll, home world,
  background, role, duplicate aptitude replacement, XP purchases and starting
  acquisitions. Home-world Wounds and grants live in `creation.d/home_world.yaml`;
  backgrounds and roles live in `creation.yaml`. These are executable creation
  payloads, not a claim that every granted ability has executable play mechanics.
- `advancement.yaml` and `advancement_purchase.yaml` supply the initial 1000 XP,
  aptitude-based prices and sequential characteristic/skill purchases. Talent
  purchases, specializations, repeatable talents and prerequisites have separate
  catalogs and generic handlers. Numeric Psy Rating and structured implants can
  satisfy prerequisites; they do not implement psychic combat or implant effects.
- `starting_equipment.yaml` supplies the starting-acquisition catalog. Inventory
  and presentation data feed the typed item catalog used by the deterministic
  Combat MVP backend: single/semi/full ranged fire, sword/knife melee, Dodge,
  Parry, Aim and Reload. The backend applies all hits, ammo and Damage through
  one checked `StateDelta`. This is not a complete weapon/combat simulator.
  Equipment granted by creation layers and starting acquisitions is issued as a
  typed `ItemInstance` whenever the item catalog recognizes the label; other labels
  stay source-labelled inventory. Every starting ranged weapon comes with two clips
  (PDF book pp. 49, 86); the engine keeps one clip loaded and does not track the
  reserve — an implementation choice, the source does not say which clip is in the gun.
  Sheets saved before this stored background weapons as labels; `CharacterSheet.from_dict`
  upgrades recognized labels deterministically and the next save persists them.
- `creation_finalization.yaml` covers the mandatory d100 Divination table.
  Immediate effects and explicit choices are executable. Result 01 is deliberately
  blocked on missing Table 8-15 data, preserving the roll. Rules written only in
  `rules` remain annotations; session-triggered effects are not automatically run.
- Studio renders the generic staged-creation state, choices, purchases, equipment
  and rich sheet information. Protocol 2.6 owns the creation types and exposes
  readiness plus the mandatory finalization's available actions, rolled result
  and pending choices. Studio renders them without DH2 rules. Result 01 remains
  blocked on the missing table; no UI action bypasses it.
- Protocol 2.7 runs a server-authoritative encounter (`.combat start`): initiative
  `1d10 + AgB` ordered high to low, ties by higher Agility then a 1d10 roll-off
  (`CH07_H011`; repeating a roll-off that ties again is an engine policy the source
  does not state, recorded under `provenance.implementation_choices`); one turn per combatant per round and a new round after
  the last turn (`CH07_H002`/`H003`/`H008`/`H009`); a full action or two
  *different* half actions, at most one Attack- and one Concentration-subtype
  action per turn (`CH07_H013`/`H014`/`H019`); Standard Attack is an Ordinary
  (+10) test (`CH07_H051`); one Reaction per round, never in
  one's own turn (`CH07_H015`/`H029`). A hit stops before damage while the
  defender may still Evade — Dodge against ranged, Dodge or Parry against melee
  (`CH07_H029`/`H061`) — and the defender's controller (player or keeper) chooses
  a reaction or declines. Using a Reaction loses a prepared Aim (`CH07_H021`).
  NPCs are keeper-controlled sheets reached through their NPC record's
  `stat_char`; `.npc create <profile> | <name>` materializes one from
  `npc_profiles.yaml` (three Troop profiles transcribed from the source PDF's
  Chapter XII pp. 485–486 and Chapter XIII p. 543, which the Neon corpus lacks).
  Damage above Wounds is Critical Damage (`CH07_H098`/`H099`/`H101`); a Troop
  NPC is taken out of the fight by any Critical Damage (Chapter XII p. 469,
  «Эффектная Гибель»), after which it gets no turns and is no longer a target.
  `state.combat` and every `action_result` are viewer-projected. The narration
  lane receives only the player-grade committed result.

Relevant coverage includes `test_creation_layers.py`, `test_creation_flow.py`,
`test_creation_finalization.py`, `test_advancement*.py`, `test_talent*.py`,
`test_repeatable_talent.py`, `test_dh2_psy_implants.py`, gateway creation/action
tests and `tests/net/test_state_creation.py`. Passing these tests does not certify
the unimplemented combat rules or replace verification against the source book.

## Deliberately not ported yet

Stage 1 does **not** invent values or mechanics for areas whose source slice has not been mapped and tested. In particular:

- point-buy character generation;
- executable mechanics for background talents, traits and special abilities beyond their structured creation payloads;
- normalized Chapter V equipment/item profiles behind the source-labelled starting inventory;
- Fate spending/burning/recovery semantics beyond the current/threshold sheet representation;
- Insanity and Corruption tracks;
- situational alternative-characteristic selection for skill checks;
- action types beyond the Combat MVP half/full attack, Aim and Reload contract;
- burst hit-location sequencing beyond automatic first-hit digit reversal;
- advanced Dodge/Parry modifiers and weapon-quality effects;
- reserve ammunition inventory and reload logistics beyond clip refill;
- righteous fury and critical-effect tables;
- conditions and duration tracking;
- psychic powers;
- surprise rounds (`CH07_H005`/`H012`), grouped initiative for identical hostiles
  (optional GM simplification in `CH07_H011`), mid-fight joins and GM reordering;
- "must be aware of the attack" for Evasion (`CH07_H029`) — a GM judgment the
  engine cannot observe; the defender's controller decides by declining;
- attack-action modifiers other than Standard Attack's +10 (bursts, Aim across a
  weapon switch — `CH07_H021` says "the next attack" while the engine binds Aim to
  the aimed weapon);
- Dodge against multiple hits (`CH07_H030`): "each degree of success cancels one
  *additional* hit" is ambiguous against the current one-hit-per-degree contract
  and awaits errata/FAQ confirmation;
- opponent profiles beyond the three Troops in `npc_profiles.yaml`: profiles need
  weapons the item catalog can represent exactly (autopistol, stub revolver,
  chain weapons, grenades are missing) and no Unnatural Characteristic traits;
  the Инфектор Штамма autogun (`O/3/–`) contradicts the catalog autogun (`CH05_H078`);
- Critical Effects (Tables 7-7…): PCs, Elite and Master NPCs past their Wounds
  keep acting until those tables exist; the engine never declares them dead;
- automatic encounter end: combatants have controllers but no sides/factions.
- semi-auto fire (`ranged_attack.semi`) is declared as a full action (`action_cost: 2`);
  live play flagged this as wrong. It is an open action-economy item to verify against
  the Short Burst section (`CH07_H048`) and fix on its own, not with transport changes.

The Divination table also requires sourced Table 8-15 data before every possible
new character can finish creation. Do not bypass that dependency by rerolling.

## Localization and distribution

Russian rulepack labels and rule text are distinct from engine messages.
`locales/ru/` currently translates six complete message domains: creation,
advancement, manual rolls, finalization, readiness and combat. Other domains use the
existing English fallback. Catalog tests pin the translated domains, key parity
and format parameters; Russian is not yet a complete engine translation.

Wheel package data explicitly includes nested rulepack YAML sidecars and Russian
catalogs. The frozen server spec already includes the entire rulepacks/locales
trees. Source tests alone do not prove an installed artifact contains those files.

Those are separate port stages. If a rule cannot be represented by the existing generic rulepack DSL, the port should identify the missing generic primitive instead of adding `if system == "dh2"` logic to the engine.

## Neon content that is not rulepack data

The `GM_RUNTIME_DIRECTIVES_RU` source and `combat_protocols` describe how our AI GM should run a table, not the Dark Heresy rules themselves. They belong in Loreweaver KP skills / `expertise` / `turn_checks` / hooks, not in `dh2.yaml`.

### Creation input guidance (protocol 2.5)

The Administratum's Scholastic Lore choice now declares RU/EN text-input guidance
in `creation_presentation.yaml`. Generic clients receive a label, placeholder and
description; the example is not a closed specialization list. Choice encoding,
free specialization validation and starting ranks are unchanged.
