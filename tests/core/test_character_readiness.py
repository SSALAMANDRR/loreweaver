from core.character_manager import CharacterSheet
from core.character_readiness import character_readiness
from core.creation_flow import load_creation_flow_spec
from core.rulepacks import load_rulepack


def _managed_sheet(*, complete_flow: bool) -> tuple[object, CharacterSheet]:
    pack = load_rulepack("dh2")
    flow = load_creation_flow_spec(pack)
    assert flow is not None
    sheet = CharacterSheet("Acolyte", "dh2")
    stage_index = len(flow.stages) if complete_flow else 1
    completed = [stage.id for stage in flow.stages] if complete_flow else [flow.stages[0].id]
    sheet.secondary_attributes["__creation_flow__"] = {
        "version": 1,
        "profile_id": "feral_world",
        "profile_reroll": {"skipped": True} if complete_flow else None,
        "stage_index": stage_index,
        "completed": completed,
        "layers": {},
    }
    return pack, sheet


def test_legacy_sheet_stays_ready_when_pack_gains_managed_creation():
    pack = load_rulepack("dh2")
    sheet = CharacterSheet("Legacy", "dh2")

    readiness = character_readiness(pack, sheet)

    assert readiness.ready is True
    assert readiness.managed is False
    assert readiness.phase == "ready"


def test_managed_sheet_is_not_ready_until_staged_creation_finishes():
    pack, sheet = _managed_sheet(complete_flow=False)

    readiness = character_readiness(pack, sheet)

    assert readiness.ready is False
    assert readiness.managed is True
    assert readiness.phase == "creation"


def test_completed_flow_waits_for_declared_finalization():
    pack, sheet = _managed_sheet(complete_flow=True)

    readiness = character_readiness(pack, sheet)

    assert readiness.ready is False
    assert readiness.phase == "finalization"


def test_completed_finalization_marks_managed_sheet_ready():
    pack, sheet = _managed_sheet(complete_flow=True)
    sheet.secondary_attributes["__creation_finalization__"] = {
        "version": 1,
        "roll": 100,
        "row_id": "ask_how_you_serve",
        "selections": {},
        "complete": True,
        "blocked_reference": "",
    }

    readiness = character_readiness(pack, sheet)

    assert readiness.ready is True
    assert readiness.managed is True
    assert readiness.phase == "ready"


def test_blocked_finalization_preserves_missing_rule_reference():
    pack, sheet = _managed_sheet(complete_flow=True)
    sheet.secondary_attributes["__creation_finalization__"] = {
        "version": 1,
        "roll": 1,
        "row_id": "mutation_without_corruption",
        "selections": {},
        "complete": False,
        "blocked_reference": "table_8_15_rudiments",
    }

    readiness = character_readiness(pack, sheet)

    assert readiness.ready is False
    assert readiness.phase == "blocked"
    assert readiness.blocked_reference == "table_8_15_rudiments"
