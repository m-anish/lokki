# Lokki Web Helper App

Static web application for configuring and managing Lokki units. Hosted via GitHub Pages, served locally by the coordinator unit, or opened directly from the filesystem.

No build step required — static HTML/CSS/JS only.

## Current pages

| Page | Purpose |
|------|---------|
| `index.html` | Fleet dashboard — all units, all channels, live status |
| `config-builder.html` | Generate and validate `config.json`; includes integrated sun times generator |

## Planned (Phase 3)

| Page | Purpose |
|------|---------|
| `scene-editor.html` | Define and push named scenes to units |
| `network-view.html` | LoRa network health, unit last-seen, signal quality |

## Deployment

GitHub Actions (`.github/workflows/gh-pages.yml`) deploys this directory to GitHub Pages automatically on push to `main`.

## Local preview

Open any `.html` file directly in a browser. For API-connected features, point the app at a running coordinator unit's IP.
