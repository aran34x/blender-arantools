# Auto-updating Aran Tools via GitHub (no separate host)

Blender 4.2+ installs this add-on as an **extension** and checks a repository
URL for updates on startup. We serve that repository for free from **GitHub
Pages** — a GitHub Action rebuilds the zip and the `index.json` every time you
push a version tag. A teammate adds one URL once; after that updates are
automatic.

**Repo:** https://github.com/aran34x/blender-arantools
**Feed URL (once Pages is live):** `https://aran34x.github.io/blender-arantools/index.json`

Pieces in this folder that make it work:
- `blender_manifest.toml` — marks the add-on as an extension.
- `.github/workflows/release.yml` — builds the zip + `index.json` and deploys to Pages.
- `publish.bat` — one-click commit + push + release tag.

---

## Current status

Already done (this is the live working copy — the **Blender 5.1** folder; the
4.5 folder has been disconnected from GitHub):

- Repo created, `main` pushed, and tag `v0.2.0` pushed (which triggered the
  build workflow).

Two settings still needed in the browser before the feed goes live — see next
section.

---

## Finish setup (you, one time — in the browser)

1. **Make the repo public.** GitHub Pages does not serve a *private* repo on
   the free plan. On `github.com/aran34x/blender-arantools`:
   **Settings ▸ General ▸ Danger Zone ▸ Change visibility ▸ Public**.
   (The add-on is GPL, so public is fine. To stay private you need GitHub
   Pro/Team.)

2. **Enable Pages via Actions.**
   **Settings ▸ Pages ▸ Build and deployment ▸ Source = GitHub Actions**.

3. **Re-run the build.** The run triggered by the `v0.2.0` tag probably failed
   on the deploy step because Pages wasn't enabled yet. Go to the **Actions**
   tab ▸ *Build & Publish Extension* ▸ latest run ▸ **Re-run all jobs**.

When it goes green, the feed is live at
`https://aran34x.github.io/blender-arantools/index.json`.

No secrets/tokens needed — the workflow uses the repo's built-in Pages
permissions.

---

## Publishing a new version (you, each release)

**Shortcut:** bump `version` in `blender_manifest.toml`, then double-click
`publish.bat` (or run `publish.bat "my message"`). It commits, pushes `main`,
and — if the version is new — creates and pushes the matching `v<version>` tag
that triggers the build. If you only changed code (same version) it just
pushes `main` without making a release.

Manual equivalent:

1. Bump `version` in `blender_manifest.toml` (e.g. `0.2.0` → `0.2.1`).
2. Commit, then tag and push:

   ```
   git commit -am "v0.2.1"
   git push origin main
   git tag v0.2.1
   git push origin v0.2.1
   ```

Each new tag rebuilds and republishes the feed; clients are offered the update.

---

## Installing (your teammate, once)

1. **Edit ▸ Preferences ▸ Get Extensions**.
2. Top-right dropdown (⌄) ▸ **Add Remote Repository**.
3. Paste: `https://aran34x.github.io/blender-arantools/index.json`
4. Tick **Check for Updates on Startup**, confirm.
5. Search "Aran Tools" in Get Extensions ▸ **Install**.

From then on, every new tag you push is offered as an update on their next
launch (or via **Check for Updates**).

---

## Notes / caveats

- **Edit in the 5.1 folder** — it's the copy wired to GitHub now. The 4.5
  folder was disconnected (`git remote remove origin`) to avoid two copies
  fighting over `main`.
- **Never change `id`** in the manifest once shared — the updater keys off it.
- The Action pins Blender `4.2.3` just to *build* the package (any 4.2+
  works). If that download URL 404s, bump `BLENDER_VERSION` / `BLENDER_SERIES`
  at the top of `.github/workflows/release.yml`.
- The panel's **Reload Addon** button reloads the `arantools` package by name;
  installed as an extension the package is renamed (`bl_ext.<repo>.arantools`)
  so that dev button may not work for teammates — they should use Blender's
  normal enable/disable or the extension updater.
- Recovery backups from the folder-wipe incident are kept at
  `C:\arantools_recovery\` until you've confirmed everything works.
