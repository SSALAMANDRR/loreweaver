# DH2 home-world creation source map

This file maps the non-characteristic part of the six core Dark Heresy Second Edition home worlds to the Russian OKP v1.8 rulebook. Chapter II is read directly from `DH_II_-OKP_-_v_1_8.pdf` because that chapter is missing from the current Neon import.

The characteristic modifiers, Fate Threshold and Emperor's Blessing remain in `rulepacks/dh2.yaml` creation profiles. The remaining home-world payload is in `rulepacks/data/dh2/creation.d/home_world.yaml` and is applied through the generic creation-layer substrate.

| Home world | Source | Wounds | Aptitude | Home-world bonus |
|---|---|---:|---|---|
| Дикий мир | p. 36 | `9+1d5` | Выносливость | Старые Пути |
| Мир-кузница | p. 38 | `8+1d5` | Интеллект | Избранный Омниссии; player chooses Искусный Стук or Длань Омниссии |
| Высокородный | p. 40 | `9+1d5` | Общительность | Бессчётное Богатство |
| Мир-улей | p. 42 | `8+1d5` | Восприятие | Бесчисленные Толпы в Металлических Горах |
| Мир-храм | p. 44 | `7+1d5` | Сила Воли | Вера в Кредо |
| Пустоторождённый | p. 46 | `7+1d5` | Интеллект | Дитя Темноты; also grants Непреклонный |

## Generic engine additions

`core.creation_layers` now supports two generic features needed here without adding any DH2 branch:

1. `creation.d/*.yaml` fragments next to the base `creation.yaml`, allowing large systems to split creation stages into auditable source files. Duplicate layer ids across files fail loudly.
2. `effects.roll_attributes`, which rolls a declared dice expression and writes the result through the pack's canonical sheet mapping. DH2 uses this for home-world Wounds.

The Forge World talent choice is mandatory and is not auto-selected. This preserves the existing rule that the engine must not make character-build choices for the player.
