# Cloudflare Pages Deployment Guide

This guide explains how to deploy the Lokki web app to Cloudflare Pages at `lokki.starstucklab.com`.

## Prerequisites

- Cloudflare account with access to `starstucklab.com` domain
- GitHub repository: `m-anish/lokki`
- Cloudflare Pages project created

## Deployment Steps

### 1. Create Cloudflare Pages Project

1. Log in to [Cloudflare Dashboard](https://dash.cloudflare.com)
2. Navigate to **Pages** in the sidebar
3. Click **Create a project**
4. Select **Connect to Git**
5. Choose **GitHub** and authorize Cloudflare
6. Select repository: `m-anish/lokki`

### 2. Configure Build Settings

**Framework preset:** None (Static HTML)

**Build configuration:**
- **Build command:** (leave empty - no build needed)
- **Build output directory:** `web/app`
- **Root directory:** `/` (repository root)

**Environment variables:** None required

### 3. Configure Custom Domain

1. After project is created, go to **Custom domains**
2. Click **Set up a custom domain**
3. Enter: `lokki.starstucklab.com`
4. Cloudflare will automatically configure DNS (CNAME record)
5. Wait for SSL certificate to provision (~1-2 minutes)

### 4. Deploy

- **Production branch:** `main`
- **Preview branches:** All branches (optional)

Every push to `main` will automatically deploy to production.
Every push to other branches creates a preview deployment.

## Configuration Files

The following files configure Cloudflare Pages behavior:

### `_headers`
Sets security and caching headers:
- Security headers (X-Frame-Options, CSP, etc.)
- Cache control for static assets
- Geolocation permissions for sun times feature

### `_redirects`
Handles URL routing:
- Root `/` redirects to `/index.html`
- Legacy URL redirects if needed

## Deployment URL

**Production:** https://lokki.starstucklab.com
**Cloudflare subdomain:** https://lokki-xyz.pages.dev (auto-generated)

## Features

✅ **Automatic deployments** - Push to main = instant deploy
✅ **Preview deployments** - Every PR gets a preview URL
✅ **Global CDN** - Fast loading worldwide
✅ **Free SSL** - HTTPS automatically configured
✅ **Unlimited bandwidth** - No usage limits on Free plan
✅ **Rollback support** - Revert to any previous deployment
✅ **Build logs** - Debug deployment issues

## Monitoring

View deployment status:
1. Cloudflare Dashboard → Pages → lokki
2. See deployment history, logs, and analytics
3. Preview URLs for each deployment

## Updating

To deploy changes:
```bash
git add .
git commit -m "Update web app"
git push origin main
```

Cloudflare automatically detects the push and deploys within ~30 seconds.

## Troubleshooting

**Issue:** Custom domain not working
- Check DNS propagation: `dig lokki.starstucklab.com`
- Verify CNAME points to Cloudflare Pages
- Wait up to 24 hours for DNS propagation

**Issue:** 404 errors
- Verify build output directory is `web/app`
- Check `_redirects` file is in `web/app/`
- Ensure all files are committed to git

**Issue:** Geolocation not working
- Check `_headers` file has `Permissions-Policy: geolocation=(self)`
- Verify HTTPS is enabled (required for geolocation API)

## Notes

- The web app is **fully static** - no server-side processing
- All tools run entirely in the browser
- Config builder uses browser APIs (File API, Geolocation API)
- Sun times generator uses SunCalc.js library
- No backend required - just static file hosting
