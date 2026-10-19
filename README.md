# storyboard-gen

Paste a script, get a storyboard of scenes and one background image per scene, all in a fixed art style.
Runs 100% locally on a Mac. No API keys, no per-image cost.

```
script.txt  ->  scenes (local LLM)  ->  background images (local image model)
```

Open [docs/how-it-works.html](docs/how-it-works.html) in a browser for the diagram and explanation. See [docs/HOW-IT-WORKS.md](docs/HOW-IT-WORKS.md) for the box-by-box walkthrough and
[docs/pipeline.excalidraw](docs/pipeline.excalidraw) for the diagram (drag the file onto https://excalidraw.com).

## Stack

| Part | Tool | Role |
|---|---|---|
| Storyboard LLM | **qwen3** via **Ollama** (local) | Splits the script into scenes and writes a background description for each |
| Image model | **Z-Image-Turbo** (Tongyi-MAI), 4-bit quantized | Generates one 1024x576 image per scene |
| Image runner | **mflux** (MLX, Apple Silicon) | Runs the image model on the Mac GPU |
| Glue | **Python 3.12** single script, **uv** | `storyboard.py` |
| Tests | pytest | `test_storyboard.py` (9 tests on scene validation) |

Why these: FLUX.1-schnell is gated on Hugging Face (needs an account and license click), Z-Image-Turbo is not.
Claude/Gemini APIs were skipped because there is no key and the goal was zero cost.

## Requirements

- Apple Silicon Mac (built and tested on M5, 16 GB RAM)
- [uv](https://docs.astral.sh/uv/) and [Ollama](https://ollama.com)
- About 20 GB free disk for the first run (12 GB download, 5.5 GB quantized copy kept in `models/`)

## Setup (once)

```bash
ollama pull qwen3        # the storyboard LLM
cd ~/Projects/storyboard-gen
uv sync                  # Python deps, creates .venv (Python 3.12)
```

The first image run downloads Z-Image-Turbo (about 12 GB) and builds the 4-bit copy automatically.

## Run

```bash
# Everything: storyboard, then images
uv run storyboard.py script.txt

# Review the scenes first, no images yet
uv run storyboard.py script.txt --storyboard-only

# Edit output/storyboard.json by hand, then generate images from it
uv run storyboard.py script.txt --from-storyboard

# Write somewhere else instead of output/script/
uv run storyboard.py script.txt --out my-episode
```

`script.txt` is any plain text file. With `--from-storyboard` the script path is still required by the CLI but is not read.

Output goes to `output/<script name>/`, one folder per script, so runs never overwrite each other
(use `--out folder` to choose a different place):

```
output/
  enchanted-forest/          from samples/enchanted-forest.txt
    storyboard.json          scenes (title + description)
    scene-01.png             one image per scene, in order
    scene-02.png
    ...
  neon-city/                 from samples/neon-city.txt
    ...
```

Running the same script again overwrites that script's folder.

Measured on an M5 / 16 GB: about 25 s per image, about 2 minutes for a 5-scene script, 4 GB peak memory.

## How it generates (short version)

1. The script text goes to qwen3 in Ollama with a system prompt and a JSON schema, so the reply is always structured scenes.
2. Code validates the JSON, removes any sentence that mentions a person, caps at 12 scenes, and saves `storyboard.json`.
3. For each scene it builds an image prompt: `STYLE. <scene description> EMPTY_SUFFIX`.
4. `mflux-generate-z-image-turbo` renders each prompt to a PNG (4 steps, 1024x576, fixed seed 42).

## The prompts

**Storyboard system prompt** (`SYSTEM_PROMPT` in `storyboard.py`):

```
You are a storyboard artist. Split the script into one scene per distinct location or time change, at most 12. Only use what the script says; do not invent extra scenes.
For each scene give a short title and a visual description of the BACKGROUND ONLY:
setting, time of day, lighting, mood, key objects, camera angle.
Describe an EMPTY scene: never mention people, characters, crew or pronouns for them.
If the script mentions a boat, show it small and empty, only in scenes on the water. Indoor scenes show only the room.
Return JSON: {"scenes": [{"title": str, "description": str}]}
```


**Image prompt** (built per scene):

```
soft watercolor illustration, painterly textures, muted pastel palette, hand-painted storybook background. <scene description> empty uninhabited scene, nobody present
```

## Demo

Two ready-made scripts in `samples/`, plus the original `sample.txt` (fisherman story):

```bash
cd ~/Projects/storyboard-gen
ollama serve                      # skip if Ollama is already running

# 1. Show the storyboard (about 10 s)
uv run storyboard.py samples/enchanted-forest.txt --storyboard-only --out output/enchanted-forest
# 2. Generate the images (about 2 min for 5 scenes)
uv run storyboard.py samples/enchanted-forest.txt --out output/enchanted-forest
open output/enchanted-forest

# Second script
uv run storyboard.py samples/neon-city.txt --out output/neon-city
open output/neon-city
```

Pre-generated results from both are already in `output/enchanted-forest/` and `output/neon-city/`, in case you want to show them without waiting.

## Change the art style

Edit `STYLE` at the top of `storyboard.py`. That one line is the main lever for matching a client's look.
Keep it to a comma-separated list of style words (medium, texture, palette, mood).
If a plain text style is not close enough, the next step is a style LoRA trained on their reference art (not built yet).

## Config (constants at the top of `storyboard.py`)

| Constant | Default | Meaning |
|---|---|---|
| `LLM_MODEL` | `qwen3` | Ollama model for scenes |
| `IMAGE_MODEL` | `z-image-turbo` | Image model name in mflux |
| `IMAGE_STEPS` | 4 | Diffusion steps (more = slower, usually finer) |
| `WIDTH`, `HEIGHT` | 1024, 576 | Image size (16:9) |
| `SEED` | 42 | Same seed for every scene |
| `MAX_SCENES` | 12 | Hard cap on scenes |
| `STYLE` | watercolor | The art style text |
| `EMPTY_SUFFIX` | `empty uninhabited scene, nobody present` | Keeps people out of backgrounds |

## Tests

```bash
uv run --with pytest pytest -q
```

Covers scene validation only (cleaning, dropping incomplete scenes, scene cap, people filter).
The LLM call and image generation are not unit tested; they were checked by running the real pipeline and viewing the images.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Ollama request ... failed (HTTP Error 500)` | Ollama's runner got stuck (happened after a memory-killed run). `pkill ollama; ollama serve`, then retry |
| `Ollama request ... failed` with connection refused | Ollama is not running. Start it: `ollama serve` |
| Image step extremely slow (minutes per image) or killed (exit 137) | The full-precision model needs about 25 GB RAM. Use the 4-bit copy in `models/z-image-turbo-q4` (the script does this automatically). If `models/` was deleted, it rebuilds |
| `cannot import name '_IntegralLike'` | Pillow 13 is not compatible with Python 3.12. `pyproject.toml` pins `pillow<13`; run `uv sync` |
| `GatedRepoError 401` for FLUX | FLUX.1-schnell needs a Hugging Face login. This project uses Z-Image-Turbo, which does not |
| Scene count differs between runs | qwen3 is not deterministic. Use `--storyboard-only`, edit `storyboard.json`, then `--from-storyboard` |

## Known limits

- **Style consistency:** the same style text and seed give a similar look, not the same village or character across scenes. For strict consistency you need a LoRA or a reference-image model.
- **No people:** backgrounds only. The pipeline removes people on purpose.
- **qwen3 is small:** scene breakdowns are decent, not Claude-quality. Always glance at the storyboard before a big batch.
- **Sequential:** one image at a time; each call reloads the model (adds a few seconds).
- **Mac only:** mflux uses Apple's MLX. For Windows/Linux the image step would need to change (e.g. diffusers + CUDA).
- **License:** check the Z-Image-Turbo license on Hugging Face before using output commercially.

## Files

```
storyboard.py          the whole pipeline
test_storyboard.py     tests
sample.txt             sample script (fisherman story)
pyproject.toml         deps: mflux, pillow<13
docs/                  HOW-IT-WORKS.md, pipeline.excalidraw
models/                4-bit image model (generated, git-ignored)
output/                results (git-ignored)
```
