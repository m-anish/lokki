# Lokki Device UI

Static HTML/CSS/JS for the on-device dashboard served by the coordinator's
own web server. Flashed to `/www/` on each coord by `utils/update.sh`;
reached at `http://<hostname>.local/` (or `http://192.168.4.1/` when the
coord is in SoftAP fallback).

This is **device firmware UI**, not a public website. The public marketing
+ docs site at [lokki.starstucklab.com](https://lokki.starstucklab.com) is
served from [`site/`](../../site/), not from this folder. See
[../CLOUDFLARE_DEPLOYMENT.md](../CLOUDFLARE_DEPLOYMENT.md) for the
migration note.

No build step. Plain static files.

## Pages

| Page | Purpose |
|------|---------|
| `index.html`           | Fleet dashboard — live status, per-unit config editor, manual override, scenes, schedule visualizer/editor, advanced settings. The "/" route serves this. |
| `config-builder.html`  | Offline config-builder for power users — generate / validate `config.json`, push to a connected device, includes the legacy sun-times JSON generator. Most config edits should happen on the dashboard's Advanced tab instead; this page is for bulk template editing and offline use. |
| `config.schema.json`   | JSON Schema. **Single source of truth** — `update.sh` flashes copies to both `/www/config.schema.json` (for the dashboard) and `/config.schema.json` (for the firmware validator). |

## Local preview

For iteration without flashing a Pico:

```bash
python3 utils/dev_server.py
open http://localhost:8088/
```

`utils/dev_server.py` stubs the `/api/*` endpoints with canned data
derived from the sample config. The dashboard renders end-to-end; saves
return a stub ack so editor UI can be exercised without persistence.

To preview AP-fallback mode (banner + LED-state-equivalents):
```bash
DEV_AP_MODE=1 python3 utils/dev_server.py
```

## File history

- `dashboard.html` was renamed to `index.html` in May 2026 to drop the
  `path == "/"` rewrite in the on-device web server. The previous
  `index.html` (a static "Setup Tools" landing page) was removed at the
  same time — first-time setup now uses the on-device boot wizard
  (SoftAP fallback + dashboard's Advanced tab).
- `_redirects` and `_headers` were Cloudflare-specific and removed when
  this directory stopped being deployed publicly.
