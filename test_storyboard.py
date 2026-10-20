import base64
import csv
import json
import struct
from types import SimpleNamespace

import pytest

import storyboard as sb

PNG = b"\x89PNG fake image bytes"


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr(sb, "BACKOFF_SECONDS", 0)


class FakeImages:
    """Stands in for client.images; `fail_times` maps scene prompt text -> failures before success."""

    def __init__(self, fail_times=None):
        self.fail_times = dict(fail_times or {})
        self.calls = []

    def _respond(self, kind, prompt, **kw):
        self.calls.append((kind, prompt, kw))
        for key, left in self.fail_times.items():
            if key in prompt and left > 0:
                self.fail_times[key] = left - 1
                raise RuntimeError("boom")
        return SimpleNamespace(data=[SimpleNamespace(b64_json=base64.b64encode(PNG).decode())])

    def generate(self, prompt, **kw):
        return self._respond("generate", prompt, **kw)

    def edit(self, prompt, **kw):
        return self._respond("edit", prompt, **kw)


class FakeChat:
    def __init__(self, scenes_by_call):
        self.scenes_by_call = list(scenes_by_call)
        self.completions = SimpleNamespace(create=self._create)
        self.prompts = []

    def _create(self, **kw):
        self.prompts.append(kw["messages"])
        content = json.dumps({"scenes": self.scenes_by_call.pop(0)})
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def make_scene(n, background="a room"):
    return {
        "number": n,
        "script_text": f"Text {n}.",
        "happening": f"Happening {n}",
        "background": f"{background} {n}",
        "characters_props": "guy, phone",
        "image_prompt": f"prompt for {background} {n}",
        "image_file": f"scene_{n:02d}.png",
        "plain_color": "",
    }


# --- splitting and scene ranges ---------------------------------------------------

def test_split_sentences_keeps_headings_separate():
    text = "Pins and Needles\n\nYou sit weird. Your foot\nis dead!\n\nWhat now? Move."
    assert sb.split_sentences(text) == [
        "Pins and Needles", "You sit weird.", "Your foot is dead!", "What now?", "Move.",
    ]


def test_normalise_ranges_cover_every_sentence_contiguously():
    raw = [{"last": 2, "happening": "a"}, {"last": 2, "happening": "dup"}, {"last": 5, "happening": "b"}]
    out = sb.normalise_ranges(raw, n=7)
    # duplicate `last` is dropped, and the final scene is stretched to cover the trailing sentences
    assert [(s["first"], s["last"]) for s in out] == [(1, 2), (3, 7)]
    assert [s["happening"] for s in out] == ["a", "b"]
    assert all(b["first"] == a["last"] + 1 for a, b in zip(out, out[1:]))


def test_normalise_ranges_clamps_out_of_range_and_empty():
    assert sb.normalise_ranges([], n=3) == []
    out = sb.normalise_ranges([{"last": 99, "happening": "x"}], n=3)
    assert [(s["first"], s["last"]) for s in out] == [(1, 3)]


def test_build_storyboard_assembles_text_from_sentences_not_from_model():
    chat = FakeChat([[
        {"last": 2, "happening": "h1", "background": "b1", "characters_props": "c1"},
        {"last": 3, "happening": "h2", "background": "b2", "characters_props": "c2"},
    ]])
    client = SimpleNamespace(chat=chat)
    scenes = sb.build_storyboard(client, "One. Two. Three.", style="STYLE", examples="")
    assert [s["script_text"] for s in scenes] == ["One. Two.", "Three."]
    assert [s["number"] for s in scenes] == [1, 2]
    assert scenes[0]["image_file"] == "scene_01.png"
    assert "STYLE" in scenes[0]["image_prompt"] and "b1" in scenes[0]["image_prompt"]
    assert "c1" not in scenes[0]["image_prompt"]  # characters never go into the image prompt


def test_build_storyboard_numbers_continue_across_chunks(monkeypatch):
    monkeypatch.setattr(sb, "CHUNK_SENTENCES", 2)
    chat = FakeChat([
        [{"last": 2, "happening": "h", "background": "b", "characters_props": "c"}],
        [{"last": 2, "happening": "h", "background": "b", "characters_props": "c"}],
    ])
    scenes = sb.build_storyboard(SimpleNamespace(chat=chat), "A. B. C. D.", style="S", examples="")
    assert [s["number"] for s in scenes] == [1, 2]
    assert [s["script_text"] for s in scenes] == ["A. B.", "C. D."]


def test_examples_are_injected_into_the_prompt():
    chat = FakeChat([[{"last": 1, "happening": "h", "background": "b", "characters_props": "c"}]])
    sb.build_storyboard(SimpleNamespace(chat=chat), "One.", style="S", examples="EXAMPLE-MARKER")
    assert "EXAMPLE-MARKER" in json.dumps(chat.prompts[0])


# --- images: resume, retry, skip, redo --------------------------------------------

def test_existing_images_are_skipped(tmp_path):
    (tmp_path / "scene_01.png").write_bytes(b"done")
    images = FakeImages()
    failed = sb.generate_images(SimpleNamespace(images=images), [make_scene(1), make_scene(2)], tmp_path, [], only=None)
    assert failed == []
    assert [c[1] for c in images.calls] == ["prompt for a room 2"]
    assert (tmp_path / "scene_01.png").read_bytes() == b"done"


def test_failed_image_is_retried_then_succeeds(tmp_path):
    images = FakeImages({"room 1": 2})  # fails twice, third attempt works
    failed = sb.generate_images(SimpleNamespace(images=images), [make_scene(1)], tmp_path, [], only=None)
    assert failed == []
    assert (tmp_path / "scene_01.png").read_bytes() == PNG


def test_image_that_keeps_failing_is_skipped_and_run_continues(tmp_path):
    images = FakeImages({"room 1": 99})
    failed = sb.generate_images(SimpleNamespace(images=images), [make_scene(1), make_scene(2)], tmp_path, [], only=None)
    assert failed == [1]
    assert not (tmp_path / "scene_01.png").exists()
    assert (tmp_path / "scene_02.png").exists()
    assert not list(tmp_path.glob("*.tmp"))  # no half-written files left behind


def test_only_regenerates_selected_scene_even_if_it_exists(tmp_path):
    (tmp_path / "scene_01.png").write_bytes(b"old1")
    (tmp_path / "scene_02.png").write_bytes(b"old2")
    images = FakeImages()
    sb.generate_images(SimpleNamespace(images=images), [make_scene(1), make_scene(2)], tmp_path, [], only={2})
    assert (tmp_path / "scene_01.png").read_bytes() == b"old1"
    assert (tmp_path / "scene_02.png").read_bytes() == PNG
    assert len(images.calls) == 1


def test_references_switch_to_edit_endpoint(tmp_path):
    ref = tmp_path / "ref.png"
    ref.write_bytes(b"ref")
    images = FakeImages()
    sb.generate_images(SimpleNamespace(images=images), [make_scene(1)], tmp_path / "out", [ref], only=None)
    assert images.calls[0][0] == "edit"
    assert len(images.calls[0][2]["image"]) == 1


def test_no_references_uses_generate(tmp_path):
    images = FakeImages()
    sb.generate_images(SimpleNamespace(images=images), [make_scene(1)], tmp_path, [], only=None)
    assert images.calls[0][0] == "generate"


# --- csv, selection, reuse --------------------------------------------------------

def read_csv(path):
    return list(csv.reader(path.open(encoding="utf-8-sig", newline="")))


def test_csv_default_has_exactly_the_six_requested_columns(tmp_path):
    (tmp_path / "scene_01.png").write_bytes(PNG)
    scenes = [{**make_scene(1), "timestamp": "00:00-00:05", "on_screen_text": "TITLE", "transitions": "Hard cut."}, make_scene(2)]
    sb.write_csv(scenes, tmp_path, tmp_path / "storyboard.csv")
    rows = read_csv(tmp_path / "storyboard.csv")
    assert rows[0] == ["Scene Number", "Script Text", "Visual Direction", "Characters/Props", "Image Prompt", "Image Filename"]
    assert rows[1][0] == "1" and rows[1][1] == "Text 1." and rows[1][3] == "guy, phone"
    assert rows[1][5] == "scene_01.png" and rows[2][5] == ""  # blank when the image is missing


def test_visual_direction_combines_everything_and_skips_none():
    scene = {**make_scene(1), "on_screen_text": "TITLE", "transitions": "Hard cut."}
    text = sb.visual_direction(scene)
    assert text == "Happening 1 Background: a room 1. On-screen text: TITLE. Transitions: Hard cut."
    quiet = sb.visual_direction({**make_scene(2), "on_screen_text": "None", "transitions": "None."})
    assert quiet == "Happening 2 Background: a room 2."


def test_full_csv_has_the_example_storyboard_columns(tmp_path):
    (tmp_path / "scene_01.png").write_bytes(PNG)
    scenes = [{**make_scene(1), "timestamp": "00:00-00:05", "on_screen_text": "TITLE", "transitions": "Hard cut."}]
    sb.write_csv(scenes, tmp_path, tmp_path / "storyboard.csv", full=True)
    rows = read_csv(tmp_path / "storyboard.csv")
    assert rows[0] == [
        "Scene", "Timestamp", "Voiceover", "What's happening", "Background", "Hand-drawn characters & props",
        "On-screen text", "Transitions/Motion", "Image Prompt", "Image Filename",
    ]
    assert rows[1][:8] == ["1", "00:00-00:05", "Text 1.", "Happening 1", "a room 1", "guy, phone", "TITLE", "Hard cut."]
    assert rows[1][9] == "scene_01.png"


@pytest.mark.parametrize("text,expected", [("3", {3}), ("1,4", {1, 4}), ("2-4", {2, 3, 4}), ("1-2,5", {1, 2, 5})])
def test_parse_scene_selection(text, expected):
    assert sb.parse_scene_selection(text) == expected


@pytest.mark.parametrize("bad", ["", "x", "0", "4-2", "1,,2"])
def test_parse_scene_selection_rejects_bad_input(bad):
    with pytest.raises(ValueError):
        sb.parse_scene_selection(bad)


def test_saved_storyboard_is_reused_and_script_change_is_detected(tmp_path):
    scenes = [make_scene(1)]
    sb.save_storyboard(tmp_path / "storyboard.json", "script v1", scenes)
    assert sb.load_storyboard(tmp_path / "storyboard.json", "script v1") == scenes
    with pytest.raises(ValueError, match="changed"):
        sb.load_storyboard(tmp_path / "storyboard.json", "script v2")


# --- plain colour scenes (no API call) ---------------------------------------------

def test_plain_colour_scene_is_painted_locally_without_api_call(tmp_path):
    scene = {**make_scene(1), "plain_color": "#EBC04A"}
    images = FakeImages()
    failed = sb.generate_images(SimpleNamespace(images=images), [scene, make_scene(2)], tmp_path, [], only=None)
    assert failed == []
    assert [c[1] for c in images.calls] == ["prompt for a room 2"]  # only scene 2 hit the API
    data = (tmp_path / "scene_01.png").read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", data[16:24])
    assert (width, height) == (1536, 1024)


def test_solid_png_has_the_requested_colour():
    import zlib
    data = sb.solid_png("#102030", 4, 2)
    idat = data[data.index(b"IDAT") + 4 : data.index(b"IEND") - 8]
    raw = zlib.decompress(idat)
    assert raw[:13] == b"\x00" + bytes([0x10, 0x20, 0x30]) * 4


def test_plain_background_flag_uses_the_configured_colour():
    chat = FakeChat([[
        {"last": 1, "happening": "h", "background": "plain flat colour", "characters_props": "c", "plain_background": True},
        {"last": 2, "happening": "h", "background": "a room", "characters_props": "c", "plain_background": False},
    ]])
    scenes = sb.build_storyboard(SimpleNamespace(chat=chat), "A. B.", style="S", examples="", plain_color="#ABCDEF")
    assert [s["plain_color"] for s in scenes] == ["#ABCDEF", ""]
    assert "#ABCDEF" in scenes[0]["image_prompt"] and "no image" in scenes[0]["image_prompt"].lower()
    assert "a room" in scenes[1]["image_prompt"]


@pytest.mark.parametrize("good", ["#ebc150", "#EBC150"])
def test_hex_colour_argument_accepts_full_hex(good):
    assert sb.hex_color(good) == "#EBC150"


@pytest.mark.parametrize("bad", ["yellow", "#FFF", "EBC150", "#GGGGGG", ""])
def test_hex_colour_argument_rejects_everything_else(bad):
    import argparse
    with pytest.raises(argparse.ArgumentTypeError):
        sb.hex_color(bad)


def test_errors_that_retrying_cannot_fix_are_not_retried(monkeypatch):
    class Rejected(Exception):
        pass

    monkeypatch.setattr(sb, "NO_RETRY", (Rejected,))
    calls = []

    def always_rejected():
        calls.append(1)
        raise Rejected("no")

    with pytest.raises(Rejected):
        sb.with_retries(always_rejected, "x")
    assert len(calls) == 1


def test_other_errors_are_retried_up_to_the_limit():
    calls = []

    def flaky():
        calls.append(1)
        raise RuntimeError("temporary")

    with pytest.raises(RuntimeError):
        sb.with_retries(flaky, "x")
    assert len(calls) == sb.API_ATTEMPTS


# --- timestamps, pacing, background reuse ------------------------------------------

def test_timestamps_are_estimated_from_word_counts(monkeypatch):
    monkeypatch.setattr(sb, "WORDS_PER_SECOND", 2.0)
    stamps = sb.estimate_timestamps(["one two three four", "five six"])
    assert stamps == ["00:00-00:02", "00:02-00:03"]


def test_timestamp_format_rolls_into_minutes():
    assert sb.format_clock(125) == "02:05"


def test_scenes_with_the_same_background_reuse_the_first_image():
    chat = FakeChat([[
        {"last": 1, "happening": "h", "background": "Living room: green couch", "characters_props": "c", "plain_background": False},
        {"last": 2, "happening": "h", "background": "Street", "characters_props": "c", "plain_background": False},
        {"last": 3, "happening": "h", "background": "living room  green couch.", "characters_props": "c", "plain_background": False},
    ]])
    scenes = sb.build_storyboard(SimpleNamespace(chat=chat), "A. B. C.", style="S", examples="")
    assert [s["reuse_of"] for s in scenes] == [0, 0, 1]


def test_reuse_can_be_turned_off():
    chat = FakeChat([[
        {"last": 1, "happening": "h", "background": "Street", "characters_props": "c", "plain_background": False},
        {"last": 2, "happening": "h", "background": "Street", "characters_props": "c", "plain_background": False},
    ]])
    scenes = sb.build_storyboard(SimpleNamespace(chat=chat), "A. B.", style="S", examples="", reuse=False)
    assert [s["reuse_of"] for s in scenes] == [0, 0]


def test_known_backgrounds_are_passed_to_later_chunks(monkeypatch):
    monkeypatch.setattr(sb, "CHUNK_SENTENCES", 1)
    row = {"last": 1, "happening": "h", "background": "Gray office", "characters_props": "c", "plain_background": False}
    chat = FakeChat([[row], [row]])
    sb.build_storyboard(SimpleNamespace(chat=chat), "A. B.", style="S", examples="")
    assert "Gray office" in json.dumps(chat.prompts[1])


def test_pacing_target_is_in_the_prompt():
    chat = FakeChat([[{"last": 1, "happening": "h", "background": "b", "characters_props": "c", "plain_background": False}]])
    sb.build_storyboard(SimpleNamespace(chat=chat), "One.", style="S", examples="", words_per_scene=17)
    assert "17 words" in json.dumps(chat.prompts[0])


def test_reused_scene_copies_the_source_image_without_an_api_call(tmp_path):
    scenes = [make_scene(1), {**make_scene(2), "reuse_of": 1}]
    scenes[0]["reuse_of"] = 0
    images = FakeImages()
    failed = sb.generate_images(SimpleNamespace(images=images), scenes, tmp_path, [], only=None)
    assert failed == []
    assert len(images.calls) == 1
    assert (tmp_path / "scene_02.png").read_bytes() == (tmp_path / "scene_01.png").read_bytes() == PNG


def test_reused_scene_fails_if_its_source_failed(tmp_path):
    scenes = [{**make_scene(1), "reuse_of": 0}, {**make_scene(2), "reuse_of": 1}]
    images = FakeImages({"room 1": 99})
    failed = sb.generate_images(SimpleNamespace(images=images), scenes, tmp_path, [], only=None)
    assert failed == [1, 2]


def test_redoing_a_source_scene_refreshes_its_copies(tmp_path):
    scenes = [{**make_scene(1), "reuse_of": 0}, {**make_scene(2), "reuse_of": 1}, {**make_scene(3), "reuse_of": 0}]
    for n in (1, 2, 3):
        (tmp_path / f"scene_{n:02d}.png").write_bytes(b"old")
    sb.generate_images(SimpleNamespace(images=FakeImages()), scenes, tmp_path, [], only={1})
    assert (tmp_path / "scene_01.png").read_bytes() == PNG
    assert (tmp_path / "scene_02.png").read_bytes() == PNG
    assert (tmp_path / "scene_03.png").read_bytes() == b"old"


def test_max_images_guard_refuses_before_spending(tmp_path):
    images = FakeImages()
    with pytest.raises(ValueError, match="max-images"):
        sb.generate_images(SimpleNamespace(images=images), [make_scene(1), make_scene(2)], tmp_path, [], only=None, max_images=1)
    assert images.calls == []


def test_max_images_does_not_count_flat_colour_or_reused_scenes(tmp_path):
    scenes = [make_scene(1), {**make_scene(2), "reuse_of": 1}, {**make_scene(3), "plain_color": "#EBC150"}]
    scenes[0]["reuse_of"] = 0
    failed = sb.generate_images(SimpleNamespace(images=FakeImages()), scenes, tmp_path, [], only=None, max_images=1)
    assert failed == []


def test_expected_scene_count_is_given_to_the_model():
    sentence = " ".join(["word"] * 70) + "."
    chat = FakeChat([[{"last": 1, "happening": "h", "background": "b", "characters_props": "c", "plain_background": False}]])
    sb.build_storyboard(SimpleNamespace(chat=chat), sentence, style="S", examples="", words_per_scene=35)
    assert "about 2 scenes" in json.dumps(chat.prompts[0])


def test_script_with_windows_bom_is_read_cleanly(tmp_path):
    f = tmp_path / "script.txt"
    f.write_bytes("﻿Hello there.".encode("utf-8"))
    assert sb.read_text(f) == "Hello there."
