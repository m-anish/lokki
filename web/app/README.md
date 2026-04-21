# Lokki Web Helper App

Static web application for configuring and managing Lokki units. Hosted via Cloudflare Pages at [lokki.starstucklab.com](https://lokki.starstucklab.com), served locally by the coordinator unit, or opened directly from the filesystem.

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

Cloudflare Pages automatically deploys this directory on push to `main`. See [../CLOUDFLARE_DEPLOYMENT.md](../CLOUDFLARE_DEPLOYMENT.md) for setup instructions.

**Production URL:** https://lokki.starstucklab.com

## Local preview

Open any `.html` file directly in a browser. For API-connected features, point the app at a running coordinator unit's IP.
