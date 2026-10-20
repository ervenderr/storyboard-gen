# storyboard-gen

Paste a script, get a storyboard, a CSV and one background image per scene in your art style.
Uses the OpenAI API. Pure Python, runs on Windows, macOS, Linux or in the cloud.
Paths with spaces need quotes: `python storyboard.py "my script.txt"`.

```
script.txt -> storyboard (scenes) -> storyboard.csv + scene_01.png, scene_02.png, ...
```

## Setup (once)

**Windows**

1. Install Python 3.12 or newer from python.org. On the first installer screen tick **"Add python.exe to PATH"**.
2. Download this repo (green Code button > Download ZIP), unzip it, open the folder, click the address bar, type `cmd` and press Enter. A command window opens in that folder.
3. Install the packages:
   ```
   pip install -r requirements.txt
   ```
4. Create the key file. In Notepad choose File > Save As, set "Save as type" to **All Files**, name it exactly `.env` (not `.env.txt`), and put one line in it:
   ```
   OPENAI_API_KEY=sk-your-key-here
   ```
   Save it in the same folder as `storyboard.py`.
5. Put your style images (up to 10) in a folder named `refs`, and your example storyboard in `examples\storyboard_examples.md`.
6. Run it (see Run below). If `python` is not found, use `py` instead.

If you prefer not to use a `.env` file, set the key for the current window only: `set OPENAI_API_KEY=sk-your-key-here` (Command Prompt) or `$env:OPENAI_API_KEY="sk-your-key-here"` (PowerShell).

**macOS / Linux**

```
pip install -r requirements.txt
echo "OPENAI_API_KEY=sk-your-key-here" > .env
```
Then add `refs/` and `examples/storyboard_examples.md` as above. (With uv: `uv sync`.)

**In the cloud**: any machine with Python 3.12+ works the same way. Set `OPENAI_API_KEY` as an environment variable instead of a `.env` file.

## Run

```
python storyboard.py my_script.txt
```

Output goes to `output/my_script/`:

| File | What |
|---|---|
| `scene_01.png`, `scene_02.png`, ... | one background image per scene |
| `storyboard.csv` | Scene Number, Script Text, Visual Direction, Characters/Props, Image Prompt, Image Filename (Visual Direction holds what is happening, the background, on-screen text and transitions) |
| `storyboard.json` | the same storyboard, used for re-runs |

With uv: `uv run storyboard.py my_script.txt`.

## Common tasks

| I want to | Command |
|---|---|
| Check the storyboard before spending on images | `python storyboard.py my_script.txt --storyboard-only` |
| Redo one scene | `python storyboard.py my_script.txt --scene 3` (also `1,4` or `2-5`) |
| Resume after a crash or failures | run the same command again: finished images are skipped |
| Split the script again from scratch | `--new-storyboard` |
| Cheaper test images | `--quality low` (`low`, `medium` (default), `high`) |
| CSV with the example storyboard's 8 columns (timestamp, on-screen text, transitions) plus the image columns | `--full-csv` |
| Never spend more than N images | `--max-images 20` (refuses to start if the run needs more) |
| Faster or slower pacing | `--words-per-scene 15` (default 35: about 13 seconds per scene) |
| One image for every scene, even repeats | `--no-reuse` |
| Change the flat-colour scene colour | `--plain-color "#EBC150"` (yellow). Scenes the storyboard calls white, green, grey, blue or red use that colour instead |
| Use another folder | `--out folder`, `--refs folder`, `--examples file` |

To change a scene's text, edit `storyboard.json`, delete that `scene_NN.png` (or use `--scene N`) and run again.

## How it works

1. The script is split into sentences. The model groups consecutive sentences into scenes and, for each, writes what is happening, the background, the hand-drawn characters and props, the on-screen text and the transitions, in the style of your example storyboard. The voiceover text is taken from the script itself, never from the model, so no words are lost or invented. Scenes always hold whole sentences. Timestamps are estimated from the word count (no audio needed).
2. Only the background goes into the image prompt, together with your reference images as the style guide, so images have no characters.
3. Scenes that are just a flat colour behind a diagram or label are painted locally in one colour (no image call, no cost). When a later scene uses the same background as an earlier one, the image is copied instead of generated again, saved under its own scene number.
4. Images are generated 3 at a time. A failed image is retried twice, then skipped, and the run carries on. At the end it lists the failed scenes; run the same command again to retry only those.

## Settings

Constants at the top of `storyboard.py`: `STORYBOARD_MODEL` (default `gpt-5-mini`), `IMAGE_MODEL` (default `chatgpt-image-latest`), `IMAGE_SIZE`, `DEFAULT_QUALITY`, `DEFAULT_PLAIN_COLOR`.

## Cost

Only scenes with a new, non-flat background call the API. In a test, 9 scenes needed 4 API images. Cost depends on quality and the current OpenAI price: check it before a full run, try `--quality low` first, and use `--max-images` as a safety cap.

## Tests

```
pip install pytest
pytest -q
```

The tests use a fake OpenAI client, so they cost nothing. They also run on Windows and Linux through GitHub Actions (`.github/workflows/test.yml`).

## Example storyboard format

`examples/storyboard_examples.md` is plain text, one scene per line:

```
Scene 2 | 00:01-00:11 | Voiceover: ... | What's happening: ... | Background: ... | Hand-drawn characters & props: ... | On-screen text: ... | Transitions/Motion: ...
```
