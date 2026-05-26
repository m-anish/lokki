#!/usr/bin/env python3
"""
gen_images.py — render marketing images for the lokki site.

Two-phase workflow, mirrored from the one used for jigawatt:

  Phase 1: draft at low quality, cheap and fast, eyeball the composition.
      python gen_images.py generate --quality low

  Phase 2: regenerate the keepers at high quality.
      python gen_images.py generate --quality high --slot hero --slot unit

  Phase 3: convert the originals/*.png to optimized site/assets/*.webp.
      python gen_images.py optimize

The prompts and per-slot render targets (WebP width and quality) live in
prompts.json so iterating on the wording doesn't require touching Python.

Requires OPENAI_API_KEY in the shell environment and Pillow for the
optimize step:

    pip install openai pillow
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path
from typing import Optional


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT  = SCRIPT_DIR.parent.parent
PROMPTS    = SCRIPT_DIR / "prompts.json"
ORIGINALS  = REPO_ROOT / "site" / "assets" / "originals"
PUBLISHED  = REPO_ROOT / "site" / "assets"
ENV_FILE   = SCRIPT_DIR / ".env"


# Auto-load utils/marketing/.env if present, so the script works without
# the caller having to remember to export OPENAI_API_KEY first. python-dotenv
# is only required for the generate step; optimize doesn't touch the key.
try:
    from dotenv import load_dotenv
    if ENV_FILE.exists():
        load_dotenv(ENV_FILE)
except ImportError:
    pass


# ---------------------------------------------------------------- helpers

def load_prompts() -> dict:
    if not PROMPTS.exists():
        sys.exit(f"prompts file not found: {PROMPTS}")
    return json.loads(PROMPTS.read_text(encoding="utf-8"))


def require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        sys.exit(f"missing required env var: {name}")
    return val


def banner(msg: str) -> None:
    print(f"\n\033[1m▸ {msg}\033[0m", flush=True)


# ---------------------------------------------------------------- generate

def cmd_generate(args: argparse.Namespace) -> int:
    """Render selected slots into site/assets/originals/<slot>.png."""
    from openai import OpenAI  # imported here so optimize-only runs don't need it

    require_env("OPENAI_API_KEY")
    client = OpenAI()

    data  = load_prompts()
    slots = data.get("slots", {})

    wanted = args.slot or list(slots.keys())
    unknown = [s for s in wanted if s not in slots]
    if unknown:
        sys.exit(f"unknown slot(s): {unknown}. known: {list(slots)}")

    ORIGINALS.mkdir(parents=True, exist_ok=True)

    for slot in wanted:
        spec   = slots[slot]
        prompt = spec["prompt"]
        size   = spec.get("size", "1024x1024")

        out = ORIGINALS / f"{slot}.png"
        if out.exists() and not args.force:
            print(f"  skip  {slot:<12}  (exists; --force to overwrite)")
            continue

        banner(f"generate  {slot}  quality={args.quality}  size={size}")
        print(f"  prompt: {prompt[:90]}…")

        resp = client.images.generate(
            model="gpt-image-1",
            prompt=prompt,
            size=size,
            quality=args.quality,
            n=1,
        )

        b64 = resp.data[0].b64_json
        out.write_bytes(base64.b64decode(b64))
        print(f"  wrote  {out.relative_to(REPO_ROOT)}  ({out.stat().st_size // 1024} KB)")

    return 0


# ---------------------------------------------------------------- optimize

def cmd_optimize(args: argparse.Namespace) -> int:
    """Resize + convert site/assets/originals/*.png to site/assets/*.webp."""
    try:
        from PIL import Image
    except ImportError:
        sys.exit("Pillow not installed. pip install pillow")

    data    = load_prompts()
    targets = data.get("_render_targets", {})
    slots   = list(data.get("slots", {}).keys())

    wanted = args.slot or slots
    PUBLISHED.mkdir(parents=True, exist_ok=True)

    for slot in wanted:
        src = ORIGINALS / f"{slot}.png"
        if not src.exists():
            print(f"  miss  {slot:<12}  (no original at {src.relative_to(REPO_ROOT)})")
            continue

        tgt = targets.get(slot, {})
        max_w = int(tgt.get("webp_width", 1600))
        q     = int(tgt.get("webp_quality", 82))

        dst = PUBLISHED / f"{slot}.webp"
        if dst.exists() and not args.force:
            print(f"  skip  {slot:<12}  (exists; --force to overwrite)")
            continue

        banner(f"optimize  {slot}  → {dst.name}  (max_w={max_w}, q={q})")

        img = Image.open(src).convert("RGB")
        if img.width > max_w:
            new_h = round(img.height * max_w / img.width)
            img   = img.resize((max_w, new_h), Image.LANCZOS)
        img.save(dst, "WEBP", quality=q, method=6)
        print(f"  wrote  {dst.relative_to(REPO_ROOT)}  ({dst.stat().st_size // 1024} KB)")

    return 0


# ---------------------------------------------------------------- cli

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    gen = sub.add_parser("generate", help="render slots to site/assets/originals/")
    gen.add_argument("--slot", action="append", help="render only this slot (repeatable)")
    gen.add_argument("--quality", choices=["low", "medium", "high", "auto"], default="low")
    gen.add_argument("--force", action="store_true", help="overwrite existing originals")
    gen.set_defaults(func=cmd_generate)

    opt = sub.add_parser("optimize", help="convert originals/*.png → site/assets/*.webp")
    opt.add_argument("--slot", action="append", help="optimize only this slot (repeatable)")
    opt.add_argument("--force", action="store_true", help="overwrite existing webp")
    opt.set_defaults(func=cmd_optimize)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
