import pytest

from storyboard import MAX_SCENES, validate_scenes


def test_valid_scenes_are_cleaned():
    out = validate_scenes({"scenes": [{"title": " A ", "description": " sunny field "}]})
    assert out == [{"title": "A", "description": "sunny field"}]


def test_incomplete_scenes_are_dropped():
    out = validate_scenes({"scenes": [{"title": "A"}, {"title": "B", "description": "x"}]})
    assert [s["title"] for s in out] == ["B"]


@pytest.mark.parametrize("bad", [{}, {"scenes": []}, {"scenes": [{"title": "A"}]}, []])
def test_bad_payload_raises(bad):
    with pytest.raises(ValueError):
        validate_scenes(bad)


def test_scene_count_is_capped():
    many = {"scenes": [{"title": str(i), "description": "d"} for i in range(MAX_SCENES + 5)]}
    assert len(validate_scenes(many)) == MAX_SCENES


def test_sentences_mentioning_people_are_stripped():
    out = validate_scenes({"scenes": [{"title": "A", "description": "A small boat rocks on waves. The fisherman is struggling. His hands shake. Lightning flashes."}]})
    assert out[0]["description"] == "A small boat rocks on waves. Lightning flashes."


def test_scene_with_only_people_is_dropped():
    out = validate_scenes({"scenes": [{"title": "A", "description": "He walks."}, {"title": "B", "description": "A quiet harbor."}]})
    assert [s["title"] for s in out] == ["B"]
