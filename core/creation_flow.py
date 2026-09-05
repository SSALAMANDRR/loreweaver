"""Generic, pack-driven orchestration for multi-stage character creation.

The existing creation primitives deliberately stay independent: profiles roll a
fresh sheet, creation layers apply explicit player choices, advancement owns XP,
and starting equipment owns free acquisitions. This module only orders those
primitives according to ``creation_flow.yaml`` and persists the current stage on
the character.

No concrete rule-system vocabulary lives here. A pack decides which layers run,
in what order, whether one layer must reuse the selected creation profile, and
where advancement / starting-equipment stages occur.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.advancement_purchase import initialize_advancement_budget
from core.character_manager import CharacterSheet
from core.creation_layers import (
    CreationLayerResult,
    apply_creation_layer,
    load_creation_layers,
    resolve_creation_layer_option,
)
from core.creation_profiles import CreationProfileResult, generate_profiled_character
from core.dice_engine import DiceRoller
from core.starting_equipment import (
    StartingEquipmentGrant,
    choose_starting_item,
    initialize_starting_equipment,
)
from core.yaml_safety import safe_load_no_aliases

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BUILTIN_DATA_ROOT = _REPO_ROOT / "rulepacks" / "data"
_STATE_KEY = "__creation_flow__"
_ALLOWED_KINDS = {"profile", "layer", "advancement", "starting_equipment"}


class CreationFlowError(ValueError):
    """A creation-flow sidecar, state transition, or player action is invalid."""


@dataclass(frozen=True)
class CreationFlowStage:
    id: str
    kind: str
    layer_id: str = ""
    option_from_profile: bool = False


@dataclass(frozen=True)
class CreationFlowSpec:
    stages: tuple[CreationFlowStage, ...]


@dataclass(frozen=True)
class CreationFlowStatus:
    profile_id: str
    stage_index: int
    stage_id: str | None
    stage_kind: str | None
    completed_stages: tuple[str, ...]
    complete: bool


@dataclass(frozen=True)
class CreationFlowStart:
    character: CharacterSheet
    profile_id: str
    attribute_rolls: Mapping[str, int]
    bonus_rolls: Mapping[str, int]
    status: CreationFlowStatus


@dataclass(frozen=True)
class CreationFlowLayerResult:
    result: CreationLayerResult
    status: CreationFlowStatus


@dataclass(frozen=True)
class CreationFlowEquipmentResult:
    grant: StartingEquipmentGrant
    status: CreationFlowStatus


def _candidate_sidecars(pack: Any, data_root: Path | None = None) -> list[Path]:
    system = str(getattr(pack, "system", "")).strip()
    if not system:
        raise CreationFlowError("pack has no system id")
    if any(part in system for part in ("/", "\\", "..")):
        raise CreationFlowError("pack system id is not safe for a creation-flow path")
    if data_root is not None:
        return [Path(data_root) / system / "creation_flow.yaml"]

    candidates: list[Path] = []
    try:
        import core.rulepacks as rulepacks

        user_root = getattr(rulepacks, "_USER_RULEPACK_DIR", None)
    except Exception:  # pragma: no cover - defensive import boundary
        user_root = None
    if user_root is not None:
        candidates.append(Path(user_root) / "data" / system / "creation_flow.yaml")
    candidates.append(_BUILTIN_DATA_ROOT / system / "creation_flow.yaml")
    return candidates


def load_creation_flow_spec(pack: Any, *, data_root: Path | None = None) -> CreationFlowSpec | None:
    path = next((candidate for candidate in _candidate_sidecars(pack, data_root) if candidate.is_file()), None)
    if path is None:
        return None
    try:
        raw = safe_load_no_aliases(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise CreationFlowError(f"could not load creation-flow sidecar {path.name!r}: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise CreationFlowError("creation-flow sidecar root must be a mapping")
    if int(raw.get("version", 1)) != 1:
        raise CreationFlowError("unsupported creation-flow sidecar version")
    unknown = set(raw) - {"version", "stages"}
    if unknown:
        raise CreationFlowError(f"creation-flow sidecar has unknown root keys {sorted(unknown)}")

    stages_raw = raw.get("stages")
    if not isinstance(stages_raw, (list, tuple)) or not stages_raw:
        raise CreationFlowError("creation-flow stages must be a non-empty list")

    known_layers = load_creation_layers(pack, data_root=data_root)
    stages: list[CreationFlowStage] = []
    seen_ids: set[str] = set()
    profile_count = 0
    for index, entry in enumerate(stages_raw):
        if not isinstance(entry, Mapping):
            raise CreationFlowError(f"creation-flow stage {index} must be a mapping")
        extra = set(entry) - {"id", "kind", "layer", "option_from_profile"}
        if extra:
            raise CreationFlowError(f"creation-flow stage {index} has unknown keys {sorted(extra)}")
        stage_id = str(entry.get("id") or "").strip()
        kind = str(entry.get("kind") or "").strip().casefold()
        if not stage_id:
            raise CreationFlowError(f"creation-flow stage {index}.id is required")
        if stage_id in seen_ids:
            raise CreationFlowError(f"creation-flow stage id {stage_id!r} is duplicated")
        seen_ids.add(stage_id)
        if kind not in _ALLOWED_KINDS:
            raise CreationFlowError(
                f"creation-flow stage {stage_id!r}.kind must be one of {sorted(_ALLOWED_KINDS)}"
            )

        layer_id = str(entry.get("layer") or "").strip()
        option_from_profile = entry.get("option_from_profile", False)
        if not isinstance(option_from_profile, bool):
            raise CreationFlowError(
                f"creation-flow stage {stage_id!r}.option_from_profile must be boolean"
            )
        if kind == "layer":
            if not layer_id:
                raise CreationFlowError(f"creation-flow layer stage {stage_id!r} requires layer")
            if layer_id not in known_layers:
                raise CreationFlowError(
                    f"creation-flow stage {stage_id!r} names unknown creation layer {layer_id!r}"
                )
        elif layer_id or option_from_profile:
            raise CreationFlowError(
                f"creation-flow stage {stage_id!r} may use layer options only when kind=layer"
            )

        if kind == "profile":
            profile_count += 1
        stages.append(
            CreationFlowStage(
                id=stage_id,
                kind=kind,
                layer_id=layer_id,
                option_from_profile=option_from_profile,
            )
        )

    if profile_count != 1 or stages[0].kind != "profile":
        raise CreationFlowError("creation-flow must contain exactly one profile stage and it must be first")
    return CreationFlowSpec(stages=tuple(stages))


def _state(character: Any) -> dict[str, Any] | None:
    secondary = getattr(character, "secondary_attributes", None)
    if not isinstance(secondary, dict):
        raise CreationFlowError("character secondary attribute storage must be a mapping")
    raw = secondary.get(_STATE_KEY)
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise CreationFlowError("creation-flow state must be a mapping")
    return raw


def _validated_state(spec: CreationFlowSpec, character: Any) -> dict[str, Any]:
    state = _state(character)
    if state is None:
        raise CreationFlowError("character creation flow is not initialized")
    if state.get("version") != 1:
        raise CreationFlowError("unsupported character creation-flow state version")
    profile_id = state.get("profile_id")
    stage_index = state.get("stage_index")
    completed = state.get("completed")
    layers = state.get("layers")
    if not isinstance(profile_id, str) or not profile_id.strip():
        raise CreationFlowError("creation-flow profile id is invalid")
    if isinstance(stage_index, bool) or not isinstance(stage_index, int):
        raise CreationFlowError("creation-flow stage index is invalid")
    if stage_index < 1 or stage_index > len(spec.stages):
        raise CreationFlowError("creation-flow stage index is out of range")
    if not isinstance(completed, list) or not all(isinstance(item, str) for item in completed):
        raise CreationFlowError("creation-flow completed stages are invalid")
    if not isinstance(layers, dict):
        raise CreationFlowError("creation-flow layer state is invalid")
    return state


def _status(spec: CreationFlowSpec, state: Mapping[str, Any]) -> CreationFlowStatus:
    index = int(state["stage_index"])
    complete = index >= len(spec.stages)
    stage = None if complete else spec.stages[index]
    return CreationFlowStatus(
        profile_id=str(state["profile_id"]),
        stage_index=index,
        stage_id=None if stage is None else stage.id,
        stage_kind=None if stage is None else stage.kind,
        completed_stages=tuple(str(item) for item in state.get("completed", [])),
        complete=complete,
    )


def creation_flow_status(
    pack: Any,
    character: Any,
    *,
    data_root: Path | None = None,
) -> CreationFlowStatus | None:
    spec = load_creation_flow_spec(pack, data_root=data_root)
    if spec is None:
        return None
    state = _state(character)
    if state is None:
        return None
    return _status(spec, _validated_state(spec, character))


def _prepare_current_stage(
    pack: Any,
    character: Any,
    spec: CreationFlowSpec,
    state: dict[str, Any],
    *,
    data_root: Path | None,
) -> None:
    while int(state["stage_index"]) < len(spec.stages):
        stage = spec.stages[int(state["stage_index"])]
        if stage.kind == "advancement":
            if initialize_advancement_budget(pack, character, data_root=data_root) is None:
                raise CreationFlowError("creation flow requires advancement rules but the pack has none")
            return
        if stage.kind == "starting_equipment":
            budget = initialize_starting_equipment(pack, character, data_root=data_root)
            if budget is None:
                raise CreationFlowError("creation flow requires starting-equipment rules but the pack has none")
            if budget.remaining > 0:
                return
            _complete_current_stage(spec, state)
            continue
        return


def _complete_current_stage(spec: CreationFlowSpec, state: dict[str, Any]) -> None:
    index = int(state["stage_index"])
    if index >= len(spec.stages):
        raise CreationFlowError("character creation flow is already complete")
    stage = spec.stages[index]
    completed = state.setdefault("completed", [])
    if not isinstance(completed, list):
        raise CreationFlowError("creation-flow completed stages are invalid")
    completed.append(stage.id)
    state["stage_index"] = index + 1


def start_creation_flow(
    pack: Any,
    profile: str,
    name: str = "",
    *,
    roller: DiceRoller | None = None,
    data_root: Path | None = None,
) -> CreationFlowStart:
    """Create a fresh profiled character and enter the first post-profile stage."""

    spec = load_creation_flow_spec(pack, data_root=data_root)
    if spec is None:
        raise CreationFlowError(f"rulepack {getattr(pack, 'system', '')!r} has no creation-flow sidecar")
    generated: CreationProfileResult = generate_profiled_character(pack, profile, name, roller=roller)
    character = generated.character
    character.secondary_attributes[_STATE_KEY] = {
        "version": 1,
        "profile_id": generated.profile_id,
        "stage_index": 1,
        "completed": [spec.stages[0].id],
        "layers": {},
    }
    state = _validated_state(spec, character)
    _prepare_current_stage(pack, character, spec, state, data_root=data_root)
    return CreationFlowStart(
        character=character,
        profile_id=generated.profile_id,
        attribute_rolls=generated.attribute_rolls,
        bonus_rolls=generated.bonus_rolls,
        status=_status(spec, state),
    )


def apply_creation_flow_layer(
    pack: Any,
    character: Any,
    option: str | None = None,
    *,
    selections: Mapping[str, Any] | None = None,
    duplicate_replacements: Mapping[str, Any] | None = None,
    roller: DiceRoller | None = None,
    data_root: Path | None = None,
) -> CreationFlowLayerResult:
    """Apply exactly the current layer stage, then advance atomically."""

    spec = load_creation_flow_spec(pack, data_root=data_root)
    if spec is None:
        raise CreationFlowError("rulepack has no creation-flow sidecar")
    state = _validated_state(spec, character)
    status = _status(spec, state)
    if status.complete:
        raise CreationFlowError("character creation flow is already complete")
    stage = spec.stages[status.stage_index]
    if stage.kind != "layer":
        raise CreationFlowError(f"current creation-flow stage {stage.id!r} is not a layer")

    target = str(option or "").strip()
    if stage.option_from_profile:
        profile_id = str(state["profile_id"])
        if target:
            resolved = resolve_creation_layer_option(pack, stage.layer_id, target, data_root=data_root)
            if resolved is None or resolved[0] != profile_id:
                raise CreationFlowError(
                    f"creation layer {stage.layer_id!r} must reuse profile option {profile_id!r}"
                )
        target = profile_id
    elif not target:
        raise CreationFlowError(f"creation layer stage {stage.id!r} requires an option")

    snapshot = copy.deepcopy(vars(character))
    try:
        result = apply_creation_layer(
            pack,
            character,
            stage.layer_id,
            target,
            selections=selections,
            duplicate_replacements=duplicate_replacements,
            roller=roller,
            data_root=data_root,
        )
        current_state = _validated_state(spec, character)
        layers = current_state["layers"]
        layers[stage.layer_id] = result.option_id
        _complete_current_stage(spec, current_state)
        _prepare_current_stage(pack, character, spec, current_state, data_root=data_root)
        return CreationFlowLayerResult(result=result, status=_status(spec, current_state))
    except Exception:
        vars(character).clear()
        vars(character).update(snapshot)
        raise


def finish_creation_advancement(
    pack: Any,
    character: Any,
    *,
    data_root: Path | None = None,
) -> CreationFlowStatus:
    """Explicitly leave the creation advancement stage, preserving unspent XP."""

    spec = load_creation_flow_spec(pack, data_root=data_root)
    if spec is None:
        raise CreationFlowError("rulepack has no creation-flow sidecar")
    state = _validated_state(spec, character)
    status = _status(spec, state)
    if status.complete:
        raise CreationFlowError("character creation flow is already complete")
    stage = spec.stages[status.stage_index]
    if stage.kind != "advancement":
        raise CreationFlowError(f"current creation-flow stage {stage.id!r} is not advancement")

    snapshot = copy.deepcopy(vars(character))
    try:
        if initialize_advancement_budget(pack, character, data_root=data_root) is None:
            raise CreationFlowError("creation advancement budget is not initialized")
        current_state = _validated_state(spec, character)
        _complete_current_stage(spec, current_state)
        _prepare_current_stage(pack, character, spec, current_state, data_root=data_root)
        return _status(spec, current_state)
    except Exception:
        vars(character).clear()
        vars(character).update(snapshot)
        raise


def choose_creation_flow_starting_item(
    pack: Any,
    character: Any,
    target: str,
    *,
    data_root: Path | None = None,
) -> CreationFlowEquipmentResult:
    """Consume one starting acquisition and finish the flow when its budget reaches zero."""

    spec = load_creation_flow_spec(pack, data_root=data_root)
    if spec is None:
        raise CreationFlowError("rulepack has no creation-flow sidecar")
    state = _validated_state(spec, character)
    status = _status(spec, state)
    if status.complete:
        raise CreationFlowError("character creation flow is already complete")
    stage = spec.stages[status.stage_index]
    if stage.kind != "starting_equipment":
        raise CreationFlowError(
            f"current creation-flow stage {stage.id!r} is not starting equipment"
        )

    snapshot = copy.deepcopy(vars(character))
    try:
        grant = choose_starting_item(pack, character, target, data_root=data_root)
        current_state = _validated_state(spec, character)
        if grant.budget.remaining == 0:
            _complete_current_stage(spec, current_state)
            _prepare_current_stage(pack, character, spec, current_state, data_root=data_root)
        return CreationFlowEquipmentResult(grant=grant, status=_status(spec, current_state))
    except Exception:
        vars(character).clear()
        vars(character).update(snapshot)
        raise
