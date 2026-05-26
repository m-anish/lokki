# utils/marketing

Image generation pipeline for the lokki marketing site at `site/`.

## one-time setup

Homebrew Python is PEP 668-locked, so use a venv:

```bash
python3 -m venv utils/marketing/.venv
utils/marketing/.venv/bin/pip install openai pillow python-dotenv
```

Save your OpenAI key in `utils/marketing/.env` (this folder's `.gitignore`
already excludes it):

```
OPENAI_API_KEY=sk-...
```

Then invoke the script through the venv's Python — `.env` is auto-loaded:

```bash
utils/marketing/.venv/bin/python utils/marketing/gen_images.py generate --quality low
```

## phase 1 — drafts

Render every slot at low quality (cheap, ~10 sec each):

```bash
python utils/marketing/gen_images.py generate --quality low
```

Or just one slot:

```bash
python utils/marketing/gen_images.py generate --quality low --slot hero
```

Open `site/assets/originals/*.png` and review. Iterate on prompts in
`utils/marketing/prompts.json`, re-run with `--force` to overwrite.

## phase 2 — finals

When the composition looks right for a slot, regenerate at high quality:

```bash
python utils/marketing/gen_images.py generate --quality high --slot hero --force
```

## phase 3 — optimize

Resize and convert the kept PNGs to WebP for the site:

```bash
python utils/marketing/gen_images.py optimize
```

Per-slot target sizes and WebP quality live under `_render_targets`
in `prompts.json`.

## slots

| Slot          | Used by               | Aspect      |
|---------------|-----------------------|-------------|
| `hero`        | `.hero` background    | 3:2 landscape |
| `hero-mobile` | `.hero` on <700px     | 2:3 portrait  |
| `mood`        | `.band.mood` background | 3:2 landscape |
| `unit`        | `.feature-image`      | 1:1 square    |
