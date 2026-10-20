"""Script -> storyboard (scenes) -> background images, using the OpenAI API.

  python storyboard.py script.txt                 # storyboard + CSV + images
  python storyboard.py script.txt --storyboard-only
  python storyboard.py script.txt --scene 3       # redo only scene 3 (also 1,4 or 2-5)
  python storyboard.py script.txt --new-storyboard  # re-split the script from scratch

Needs OPENAI_API_KEY (environment variable or a .env file). Style reference images go in refs/.
"""
import argparse
import base64
import csv
import hashlib
import json
import os
import re
import shutil
import sys
import struct
import time
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import ExitStack
from pathlib import Path

from dotenv import load_dotenv
from openai import AuthenticationError, BadRequestError, NotFoundError, OpenAI, OpenAIError, PermissionDeniedError

STORYBOARD_MODEL = "gpt-5-mini"
IMAGE_MODEL = "chatgpt-image-latest"
IMAGE_SIZE = "1536x1024"
DEFAULT_QUALITY = "medium"  # low | medium | high | auto: the main cost lever
CHUNK_SENTENCES = 40  # sentences sent to the LLM per call
API_ATTEMPTS = 3  # first try + 2 retries
BACKOFF_SECONDS = 2
DEFAULT_WORKERS = 3
NO_RETRY = (AuthenticationError, PermissionDeniedError, BadRequestError, NotFoundError)  # retrying cannot fix these
MAX_REFS = 10
REF_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
HEX_COLOR = re.compile(r"#[0-9a-fA-F]{6}")
DEFAULT_PLAIN_COLOR = "#EBC150"  # the flat yellow in the example videos
IMAGE_WIDTH, IMAGE_HEIGHT = (int(v) for v in IMAGE_SIZE.split("x"))
DEFAULT_REFS_DIR = Path("refs")
DEFAULT_EXAMPLES = Path("examples") / "storyboard_examples.md"
WORDS_PER_SECOND = 2.7  # measured from the example videos; used to estimate timestamps without audio
DEFAULT_WORDS_PER_SCENE = 35  # matches the first example storyboard (about 13 s per scene)
CSV_COLUMNS = ["Scene Number", "Script Text", "Visual Direction", "Characters/Props", "Image Prompt", "Image Filename"]
FULL_CSV_COLUMNS = [  # the layout of the example storyboards, plus the image columns
    "Scene", "Timestamp", "Voiceover", "What's happening", "Background", "Hand-drawn characters & props",
    "On-screen text", "Transitions/Motion", "Image Prompt", "Image Filename",
]

STYLE_WITH_REFS = (
    "Match the art style of the reference images exactly: thick bold black outlines, completely flat solid colours "
    "with no gradients, no texture, no shading and no lighting effects, very simple shapes and minimal detail, "
    "like a simple cartoon."
)
STYLE_WITHOUT_REFS = (
    "Simple 2D cartoon: thick bold black outlines, completely flat solid colours with no gradients, texture or shading, "
    "very simple shapes and minimal detail."
)
IMAGE_RULES = "Background only: do not draw any characters, people, hands or text. Wide landscape composition."

SYSTEM_PROMPT = """You are a storyboard artist for animated explainer videos.
You get a script as numbered sentences. Group CONSECUTIVE sentences into scenes. Cover every sentence, in order.
Aim for about {WORDS} words of voiceover per scene. Start a new scene when the setting or the visual idea changes.
Write short, plain, concrete notes like the examples. For each scene return:
- last: the number of the last sentence in the scene
- happening: what is happening on screen, one short line
- background: the environment only (a room, street, office), a few words. No characters, props, diagrams, anatomy,
  icons, labels or text: the editors draw those by hand. When a scene is in the same place as an earlier scene,
  reuse that earlier background text exactly.
- plain_background: true if the scene is only a flat colour (for a diagram, label, text or close-up; background then
  names the colour, e.g. "Yellow."), false if there is a real environment
- characters_props: the characters, props, diagrams and icons the editors draw by hand
- on_screen_text: the exact text shown on screen, or "None"
- transitions: transitions and motion, or "None"
Return JSON only."""

SCENES_SCHEMA = {
    "type": "object",
    "properties": {
        "scenes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "last": {"type": "integer"},
                    "happening": {"type": "string"},
                    "background": {"type": "string"},
                    "plain_background": {"type": "boolean"},
                    "characters_props": {"type": "string"},
                    "on_screen_text": {"type": "string"},
                    "transitions": {"type": "string"},
                },
                "required": ["last", "happening", "background", "plain_background", "characters_props", "on_screen_text", "transitions"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["scenes"],
    "additionalProperties": False,
}


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")  # utf-8-sig: Windows Notepad adds a BOM


def log(message: str) -> None:
    print(message, flush=True)


def with_retries(fn, what: str):
    for attempt in range(1, API_ATTEMPTS + 1):
        try:
            return fn()
        except Exception as e:  # any API/network error: retry, then let the caller skip
            if attempt == API_ATTEMPTS or isinstance(e, NO_RETRY):
                raise
            log(f"  {what}: attempt {attempt} failed ({type(e).__name__}: {str(e)[:120]}), retrying")
            time.sleep(BACKOFF_SECONDS * attempt)


# --- script -> sentences -> scenes -------------------------------------------------

def split_sentences(text: str) -> list[str]:
    sentences = []
    for paragraph in re.split(r"\n\s*\n", text.strip()):
        joined = " ".join(line.strip() for line in paragraph.splitlines() if line.strip())
        sentences.extend(s for s in re.split(r"(?<=[.!?])[\"')\]]*\s+", joined) if s.strip())
    return sentences


def normalise_ranges(raw: list[dict], n: int) -> list[dict]:
    """Turn the model's `last` numbers into contiguous ranges covering sentences 1..n exactly."""
    out, prev_end = [], 0
    for scene in sorted(raw, key=lambda s: s["last"]):
        end = min(scene["last"], n)
        if end <= prev_end:
            continue
        out.append({**scene, "first": prev_end + 1, "last": end})
        prev_end = end
    if out:
        out[-1] = {**out[-1], "last": n}
    return out


def scene_prompt(style: str, background: str, plain_color: str = "") -> str:
    if plain_color:
        return f"Solid colour {plain_color} (painted by the tool, no image model call)"
    return f"{style} {IMAGE_RULES} Scene: {background}"


def hex_color(value: str) -> str:
    if not HEX_COLOR.fullmatch(value or ""):
        raise argparse.ArgumentTypeError(f"{value!r} is not a hex colour like #EBC150")
    return value.upper()


def solid_png(color: str, width: int = IMAGE_WIDTH, height: int = IMAGE_HEIGHT) -> bytes:
    """A flat-colour PNG using only the standard library (no Pillow needed)."""
    rgb = bytes(int(color[i : i + 2], 16) for i in (1, 3, 5))
    row = b"\x00" + rgb * width

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(row * height, 9)) + chunk(b"IEND", b"")


def background_key(text: str) -> str:
    return re.sub(r"\W+", " ", text.lower()).strip()


def format_clock(seconds: int) -> str:
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def estimate_timestamps(texts: list[str]) -> list[str]:
    stamps, words = [], 0
    for text in texts:
        n = len(text.split())
        stamps.append(f"{format_clock(round(words / WORDS_PER_SECOND))}-{format_clock(round((words + n) / WORDS_PER_SECOND))}")
        words += n
    return stamps


def storyboard_chunk(client, sentences: list[str], examples: str, words_per_scene: int = DEFAULT_WORDS_PER_SCENE,
                     known_backgrounds: list[str] | None = None) -> list[dict]:
    numbered = "\n".join(f"{i}. {s}" for i, s in enumerate(sentences, 1))
    words = sum(len(x.split()) for x in sentences)
    numbered += f"\n\nThis part has {words} words: make about {max(1, round(words / words_per_scene))} scenes."
    if known_backgrounds:
        numbered += "\n\nBackgrounds already used earlier (reuse the exact text when the same place appears again):\n"
        numbered += "\n".join(f"- {b}" for b in known_backgrounds)
    system = SYSTEM_PROMPT.replace("{WORDS}", str(words_per_scene))
    if examples:
        system += "\n\nMatch these example storyboards exactly for how scenes are split, the pacing and the level of detail:\n" + examples

    def call():
        resp = client.chat.completions.create(
            model=STORYBOARD_MODEL,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": numbered}],
            response_format={"type": "json_schema", "json_schema": {"name": "scenes", "strict": True, "schema": SCENES_SCHEMA}},
        )
        return json.loads(resp.choices[0].message.content)["scenes"]

    ranges = normalise_ranges(with_retries(call, "storyboard"), len(sentences))
    if not ranges:
        raise ValueError("The model returned no usable scenes for this part of the script")
    return [{**r, "text": " ".join(sentences[r["first"] - 1 : r["last"]])} for r in ranges]


def build_storyboard(client, script: str, style: str, examples: str, plain_color: str = DEFAULT_PLAIN_COLOR,
                     words_per_scene: int = DEFAULT_WORDS_PER_SCENE, reuse: bool = True) -> list[dict]:
    sentences = split_sentences(script)
    if not sentences:
        raise ValueError("The script is empty")
    parts, known = [], []
    for start in range(0, len(sentences), CHUNK_SENTENCES):
        chunk = storyboard_chunk(client, sentences[start : start + CHUNK_SENTENCES], examples, words_per_scene, known)
        parts.extend(chunk)
        known.extend(b for b in dict.fromkeys(p["background"].strip() for p in chunk if not p.get("plain_background")) if b not in known)
    width = max(2, len(str(len(parts))))
    stamps = estimate_timestamps([p["text"] for p in parts])
    scenes, first_seen = [], {}
    for i, p in enumerate(parts, 1):
        background = p["background"].strip()
        plain = bool(p.get("plain_background"))
        key = background_key(background)
        reuse_of = first_seen.get(key, 0) if reuse and not plain else 0
        if not plain:
            first_seen.setdefault(key, i)
        scenes.append({
            "number": i,
            "timestamp": stamps[i - 1],
            "script_text": p["text"],
            "happening": p["happening"].strip(),
            "background": background,
            "characters_props": p["characters_props"].strip(),
            "on_screen_text": p.get("on_screen_text", "").strip(),
            "transitions": p.get("transitions", "").strip(),
            "image_prompt": scene_prompt(style, background, plain_color if plain else ""),
            "image_file": f"scene_{i:0{width}d}.png",
            "plain_color": plain_color if plain else "",
            "reuse_of": reuse_of,
        })
    return scenes


def script_hash(script: str) -> str:
    return hashlib.sha256(script.encode("utf-8")).hexdigest()


def save_storyboard(path: Path, script: str, scenes: list[dict]) -> None:
    path.write_text(json.dumps({"script_sha256": script_hash(script), "scenes": scenes}, indent=2), encoding="utf-8")


def load_storyboard(path: Path, script: str) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("script_sha256") != script_hash(script):
        raise ValueError(f"The script changed since {path} was made. Re-run with --new-storyboard to split it again.")
    return data["scenes"]


# --- images ------------------------------------------------------------------------

def generate_image(client, scene: dict, refs: list[Path], out: Path, quality: str) -> None:
    prompt = scene["image_prompt"]

    def call():
        with ExitStack() as stack:
            if refs:
                files = [stack.enter_context(open(p, "rb")) for p in refs]
                return client.images.edit(model=IMAGE_MODEL, image=files, prompt=prompt, size=IMAGE_SIZE, quality=quality)
            return client.images.generate(model=IMAGE_MODEL, prompt=prompt, size=IMAGE_SIZE, quality=quality, n=1)

    if scene.get("plain_color"):
        data = solid_png(scene["plain_color"])
    else:
        data = base64.b64decode(with_retries(call, out.name).data[0].b64_json)
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(out)  # a crash never leaves a half-written scene_NN.png


def copy_image(src: Path, out: Path) -> None:
    tmp = out.with_name(out.name + ".tmp")
    shutil.copyfile(src, tmp)
    tmp.replace(out)


def plan_images(scenes: list[dict], out_dir: Path, only: set[int] | None) -> tuple[list[dict], list[dict]]:
    """Split the work into scenes that need their own image and scenes that copy an earlier scene's image."""
    def done(s):
        target = out_dir / s["image_file"]
        return target.exists() and target.stat().st_size > 0

    if only is None:
        todo = [s for s in scenes if not done(s)]
    else:  # redoing a scene also refreshes the scenes that share its image
        todo = [s for s in scenes if s["number"] in only or s.get("reuse_of") in only]
    own = [s for s in todo if not s.get("reuse_of") or (only and s["number"] in only)]
    return own, [s for s in todo if s not in own]


def generate_images(client, scenes: list[dict], out_dir: Path, refs: list[Path], only: set[int] | None,
                    quality: str = DEFAULT_QUALITY, workers: int = DEFAULT_WORKERS, max_images: int | None = None) -> list[int]:
    """Returns the scene numbers that failed after all retries. Never raises for one bad image."""
    out_dir.mkdir(parents=True, exist_ok=True)
    own, copies = plan_images(scenes, out_dir, only)
    paid = [s for s in own if not s.get("plain_color")]
    log(f"{len(paid)} image(s) from the API, {len(own) - len(paid)} flat-colour, {len(copies)} reused")
    if max_images is not None and len(paid) > max_images:
        raise ValueError(f"This run would make {len(paid)} API images, over --max-images {max_images}. Nothing was generated.")

    failed = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(generate_image, client, s, refs, out_dir / s["image_file"], quality): s for s in own}
        for fut in as_completed(futures):
            s = futures[fut]
            try:
                fut.result()
                log(f"  scene {s['number']}: saved {s['image_file']}")
            except Exception as e:
                failed.append(s["number"])
                log(f"  scene {s['number']}: FAILED, skipping ({type(e).__name__}: {str(e)[:160]})")

    by_number = {s["number"]: s for s in scenes}
    for s in copies:
        src = out_dir / by_number[s["reuse_of"]]["image_file"]
        if src.exists() and src.stat().st_size > 0:
            copy_image(src, out_dir / s["image_file"])
            log(f"  scene {s['number']}: reused image of scene {s['reuse_of']}")
        else:
            failed.append(s["number"])
            log(f"  scene {s['number']}: skipped, the image it reuses (scene {s['reuse_of']}) is missing")
    return sorted(failed)


# --- csv, cli helpers --------------------------------------------------------------

def visual_direction(scene: dict) -> str:
    parts = [f"{scene['happening']} Background: {scene['background']}."]
    for label, key in (("On-screen text", "on_screen_text"), ("Transitions", "transitions")):
        value = scene.get(key, "").strip().rstrip(".")
        if value and value.lower() != "none":
            parts.append(f"{label}: {value}.")
    return " ".join(parts).replace("..", ".")


def write_csv(scenes: list[dict], out_dir: Path, path: Path, full: bool = False) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:  # utf-8-sig so Excel on Windows reads it right
        writer = csv.writer(f)
        writer.writerow(FULL_CSV_COLUMNS if full else CSV_COLUMNS)
        for s in scenes:
            image = s["image_file"] if (out_dir / s["image_file"]).exists() else ""
            if full:
                writer.writerow([
                    s["number"], s.get("timestamp", ""), s["script_text"], s["happening"], s["background"],
                    s["characters_props"], s.get("on_screen_text", ""), s.get("transitions", ""), s["image_prompt"], image,
                ])
            else:
                writer.writerow([s["number"], s["script_text"], visual_direction(s), s["characters_props"], s["image_prompt"], image])


def parse_scene_selection(text: str) -> set[int]:
    selected = set()
    for part in text.split(","):
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part.strip())
        if not m:
            raise ValueError(f"Bad --scene value {text!r}. Use 3, 1,4 or 2-5")
        lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
        if lo < 1 or hi < lo:
            raise ValueError(f"Bad --scene range {part!r}")
        selected.update(range(lo, hi + 1))
    return selected


def find_refs(refs_dir: Path) -> list[Path]:
    if not refs_dir.is_dir():
        return []
    refs = sorted(p for p in refs_dir.iterdir() if p.suffix.lower() in REF_SUFFIXES)
    if len(refs) > MAX_REFS:
        log(f"Found {len(refs)} reference images, using the first {MAX_REFS}")
    return refs[:MAX_REFS]


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Script to storyboard, CSV and background images")
    ap.add_argument("script", type=Path)
    ap.add_argument("--out", type=Path, help="output folder (default: output/<script name>)")
    ap.add_argument("--refs", type=Path, default=DEFAULT_REFS_DIR, help="folder with style reference images")
    ap.add_argument("--examples", type=Path, default=DEFAULT_EXAMPLES, help="example storyboards the model should imitate")
    ap.add_argument("--full-csv", action="store_true", help="CSV with the 8 example-storyboard columns (timestamp, on-screen text, transitions) plus the image columns")
    ap.add_argument("--storyboard-only", action="store_true")
    ap.add_argument("--new-storyboard", action="store_true", help="split the script again instead of reusing storyboard.json")
    ap.add_argument("--scene", help="regenerate only these scenes, e.g. 3, 1,4 or 2-5")
    ap.add_argument("--plain-color", type=hex_color, default=DEFAULT_PLAIN_COLOR, help="colour for flat-colour scenes (no image call)")
    ap.add_argument("--words-per-scene", type=int, default=DEFAULT_WORDS_PER_SCENE, help="pacing: about this many voiceover words per scene")
    ap.add_argument("--no-reuse", action="store_true", help="make a separate image for every scene, even when backgrounds repeat")
    ap.add_argument("--max-images", type=int, help="refuse to run if more than this many API images would be made")
    ap.add_argument("--quality", default=DEFAULT_QUALITY, choices=["low", "medium", "high", "auto"])
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")  # never crash on a character the console cannot show
    load_dotenv(encoding="utf-8-sig")
    if not os.environ.get("OPENAI_API_KEY"):
        sys.exit("OPENAI_API_KEY is not set. Put it in a .env file (OPENAI_API_KEY=...) or set it as an environment variable.")
    try:
        only = parse_scene_selection(args.scene) if args.scene else None
        script = read_text(args.script)
    except (ValueError, OSError) as e:
        sys.exit(str(e))

    out_dir = args.out or Path("output") / args.script.stem
    out_dir.mkdir(parents=True, exist_ok=True)
    board, csv_path = out_dir / "storyboard.json", out_dir / "storyboard.csv"
    client = OpenAI()
    refs = find_refs(args.refs)
    style = STYLE_WITH_REFS if refs else STYLE_WITHOUT_REFS
    log(f"Style references: {len(refs)}" + ("" if refs else " (none found in refs/, using a generic cartoon style)"))

    try:
        if board.exists() and not args.new_storyboard:
            scenes = load_storyboard(board, script)
            log(f"Reusing {board} ({len(scenes)} scenes)")
        else:
            examples = read_text(args.examples) if args.examples.exists() else ""
            scenes = build_storyboard(client, script, style, examples, args.plain_color, args.words_per_scene, not args.no_reuse)
            save_storyboard(board, script, scenes)
            log(f"Storyboard: {len(scenes)} scenes -> {board}")
    except (ValueError, OpenAIError) as e:
        sys.exit(f"Storyboard step failed: {e}")

    if only and max(only) > len(scenes):
        sys.exit(f"--scene {max(only)} is out of range: this storyboard has {len(scenes)} scenes")
    write_csv(scenes, out_dir, csv_path, args.full_csv)
    if args.storyboard_only:
        log(f"CSV -> {csv_path}")
        return 0

    try:
        failed = generate_images(client, scenes, out_dir, refs, only, args.quality, args.workers, args.max_images)
    except ValueError as e:
        sys.exit(str(e))
    write_csv(scenes, out_dir, csv_path, args.full_csv)
    log(f"Done. Images and CSV in {out_dir}")
    if failed:
        log(f"Scenes that failed: {', '.join(map(str, failed))}. Re-run the same command to retry just those.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
