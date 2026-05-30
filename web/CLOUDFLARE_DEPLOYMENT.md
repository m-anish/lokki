# `web/app/` — no longer deployed to Cloudflare Pages

This folder used to be the Cloudflare Pages build output for
`lokki.starstucklab.com`. As of May 2026, the public site has moved:

- **`lokki.starstucklab.com`** now serves the marketing site at
  [`site/`](../site/) — see [`site/CLOUDFLARE_DEPLOYMENT.md`](../site/CLOUDFLARE_DEPLOYMENT.md)
  for the current Pages configuration.
- The contents of this folder (`index.html` — the dashboard, formerly
  `dashboard.html`; `config-builder.html`; `vendor/`; etc.) are still
  flashed to each coordinator unit by `utils/update.sh` and served
  locally by the coordinator's on-device web server at
  `http://<hostname>.local/`. `_headers` and `_redirects` were
  Cloudflare-specific and removed when this directory stopped being
  deployed publicly.

In other words: this is **device firmware UI**, not a public website
anymore. The files here are bundled into the firmware image and
operate on the LAN; they do not need a public DNS or a Pages deploy.

If you want to host this dashboard publicly for any reason (demos,
documentation, etc.), create a separate Pages project pointed at
`web/app` and use a different subdomain (e.g. `app.lokki.starstucklab.com`).
Do not point `lokki.starstucklab.com` back at this folder without
also updating the marketing site's hosting setup.

## Operating notes (still relevant)

- The dashboard is fully static — no server-side processing on Cloudflare
- All tools run in the browser (File API, Geolocation API, SunCalc.js)
- `_headers` sets security headers and Permissions-Policy for geolocation
- `_redirects` is reserved for future URL rewrites; currently empty
