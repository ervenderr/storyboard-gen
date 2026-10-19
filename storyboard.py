"""Script -> storyboard scenes (local Ollama LLM) -> background images (local Z-Image-Turbo via mflux).

Usage:
  uv run storyboard.py script.txt                  # storyboard + images
  uv run storyboard.py script.txt --storyboard-only  # review scenes first
  uv run storyboard.py script.txt --from-storyboard  # images from edited storyboard.json
"""
import argparse
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

OLLAMA_URL = "http://localhost:11434/api/chat"
LLM_MODEL = "qwen3"
IMAGE_MODEL = "z-image-turbo"
IMAGE_MODEL_DIR = Path(__file__).parent / "models" / "z-image-turbo-q4"
IMAGE_STEPS = 4
WIDTH, HEIGHT = 1024, 576
SEED = 42
MAX_SCENES = 12
# Placeholder style, swap for the client's art style.
STYLE = "soft watercolor illustration, painterly textures, muted pastel palette, hand-painted storybook background"
# Image model ignores negatives (and "no people" can prime people), so ask for emptiness instead.
EMPTY_SUFFIX = "empty uninhabited scene, nobody present"
PEOPLE_RE = re.compile(
    r"\b(fisherman|fishermen|man|men|woman|women|person|people|figure|silhouette of a|character|child|children|crew|sailor|captain|he|she|they|his|her|their)\b",
    re.I,
)

SYSTEM_PROMPT = f"""You are a storyboard artist. Split the script into one scene per distinct location or time change, at most {MAX_SCENES}. Only use what the script says; do not invent extra scenes.
For each scene give a short title and a visual description of the BACKGROUND ONLY:
setting, time of day, lighting, mood, key objects, camera angle.
Describe an EMPTY scene: never mention people, characters, crew or pronouns for them.
If the script mentions a boat, show it small and empty, only in scenes on the water. Indoor scenes show only the room.
Return JSON: {{"scenes": [{{"title": str, "description": str}}]}}"""

SCENES_SCHEMA = {
    "type": "object",
    "properties": {
        "scenes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"title": {"type": "string"}, "description": {"type": "string"}},
                "required": ["title", "description"],
            },
        }
    },
    "required": ["scenes"],
}


def strip_people(description: str) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", description)
    return " ".join(x for x in sentences if not PEOPLE_RE.search(x))


def validate_scenes(data: dict) -> list[dict]:
    scenes = data.get("scenes") if isinstance(data, dict) else None
    if not scenes:
        raise ValueError("LLM returned no scenes")
    cleaned = [
        {"title": str(s["title"]).strip(), "description": strip_people(str(s["description"]).strip())}
        for s in scenes
        if isinstance(s, dict) and s.get("title") and s.get("description")
    ]
    cleaned = [s for s in cleaned if s["description"]]
    if not cleaned:
        raise ValueError("LLM scenes were missing title/description")
    return cleaned[:MAX_SCENES]


def make_storyboard(script: str) -> list[dict]:
    body = json.dumps({
        "model": LLM_MODEL,
        "stream": False,
        "think": False,
        "format": SCENES_SCHEMA,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": script},
        ],
    }).encode()
    req = urllib.request.Request(OLLAMA_URL, body, {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            content = json.load(resp)["message"]["content"]
    except OSError as e:
        sys.exit(f"Ollama request to {OLLAMA_URL} failed ({e}). Is `ollama serve` running? If it is, restart it.")
    return validate_scenes(json.loads(content))


def ensure_quantized_model() -> None:
    # Loading the full model peaks at ~25 GB RAM; a saved 4-bit copy peaks at ~4 GB.
    if IMAGE_MODEL_DIR.exists():
        return
    print("First run: building 4-bit model copy (downloads ~12 GB once)...")
    subprocess.run(
        ["mflux-save", "--model", IMAGE_MODEL, "--quantize", "4", "--path", str(IMAGE_MODEL_DIR)],
        check=True,
    )


def generate_image(prompt: str, out: Path) -> None:
    subprocess.run(
        [
            "mflux-generate-z-image-turbo",
            "--model", str(IMAGE_MODEL_DIR), "--base-model", IMAGE_MODEL,
            "--steps", str(IMAGE_STEPS), "--width", str(WIDTH), "--height", str(HEIGHT),
            "--seed", str(SEED), "--prompt", prompt, "--output", str(out),
        ],
        check=True,
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("script", type=Path)
    ap.add_argument("--out", type=Path, help="output folder (default: output/<script name>)")
    ap.add_argument("--storyboard-only", action="store_true")
    ap.add_argument("--from-storyboard", action="store_true")
    args = ap.parse_args()

    if args.out is None:
        args.out = Path("output") / args.script.stem
    args.out.mkdir(parents=True, exist_ok=True)
    board = args.out / "storyboard.json"

    if args.from_storyboard:
        scenes = validate_scenes(json.loads(board.read_text()))
    else:
        scenes = make_storyboard(args.script.read_text())
        board.write_text(json.dumps({"scenes": scenes}, indent=2))
        print(f"Storyboard: {len(scenes)} scenes -> {board}")
        for i, s in enumerate(scenes, 1):
            print(f"  {i:02d}. {s['title']}")

    if args.storyboard_only:
        return

    ensure_quantized_model()
    for i, s in enumerate(scenes, 1):
        out = args.out / f"scene-{i:02d}.png"
        print(f"[{i}/{len(scenes)}] {s['title']}")
        generate_image(f"{STYLE}. {s['description']} {EMPTY_SUFFIX}", out)
    print(f"Done -> {args.out}/")


if __name__ == "__main__":
    main()
