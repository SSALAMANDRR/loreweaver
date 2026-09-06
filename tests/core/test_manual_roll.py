from __future__ import annotations

import pytest

from core.manual_roll import ManualRollError, manual_roll_detail, parse_manual_dice_expression


def test_manual_d100_preserves_the_natural_face_and_marks_the_source():
    spec = parse_manual_dice_expression("1d100")
    assert spec.count == 1 and spec.sides == 100

    rolled = manual_roll_detail("1d100", [37])

    assert rolled.dice == (37,)
    assert rolled.total == 37
    assert rolled.modifiers == {"source": "manual"}


def test_manual_creation_expression_keeps_the_highest_faces_and_flat_modifier():
    rolled = manual_roll_detail("3d10kh2+20", [3, 9, 7])

    assert rolled.dice == (9, 7)
    assert rolled.total == 36
    assert rolled.modifiers == {
        "source": "manual",
        "modifier": 20,
        "dice_all": [3, 9, 7],
    }


def test_manual_creation_expression_keeps_the_lowest_faces():
    rolled = manual_roll_detail("3d10kl2+20", [3, 9, 7])

    assert rolled.dice == (3, 7)
    assert rolled.total == 30
    assert rolled.modifiers["dice_all"] == [3, 9, 7]


@pytest.mark.parametrize(
    ("expression", "faces"),
    [
        ("1d100", [0]),
        ("1d100", [101]),
        ("2d10+20", [7]),
        ("2d10+20", [7, 11]),
    ],
)
def test_manual_faces_fail_closed_when_the_physical_roll_cannot_match(expression, faces):
    with pytest.raises(ManualRollError):
        manual_roll_detail(expression, faces)


@pytest.mark.parametrize("expression", ["4df", "5d6!", "7d10>=8", "2d20kh1+1d4"])
def test_manual_substrate_refuses_semantics_it_cannot_reconstruct(expression):
    with pytest.raises(ManualRollError):
        parse_manual_dice_expression(expression)
