# Auto-updating Aran Tools via GitHub (no separate host)

Blender 4.2+ installs this add-on as an **extension** and checks a repository
URL for updates on startup. We serve that repository for free from **GitHub
Pages** — a GitHub Action rebuilds the zip and the `index.json` every time you
push to `main`. A teammate adds one URL once; after that updates are
automatic.

**Repo:** https://github.com/aran34x/blender-arantools
**Feed URL (once Pages is live):** `https://aran34x.github.io/blender-arantools/index.json`

Pieces in this folder that make it work:
- `blender_manifest.toml` — marks the add-on as an extension.
- `.github/workflows/release.yml` — builds the zip + `index.json` and deploys to Pages.
- `publish.bat` — one-click commit + push + release tag.

---

## Current status

This is the live working copy — the **Blender 5.1** folder; the 4.5 folder has
been disconnected from GitHub. The repo is **public**, Pages **Source = GitHub
Actions**, and the workflow runs on every push to `main`.

If you ever set this up fresh, the one-time browser steps are:

1. **Make the repo public.** GitHub Pages does not serve a *private* repo on
   the free plan. **Settings ▸ General ▸ Danger Zone ▸ Change visibility ▸
   Public**. (The add-on is GPL, so public is fine; to stay private you need
   GitHub Pro/Team.)

2. **Enable Pages via Actions.** **Settings ▸ Pages ▸ Build and deployment ▸
   Source = GitHub Actions**.

That's it — no secrets/tokens. Pushing `main` then builds and deploys, and the
feed goes live at `https://aran34x.github.io/blender-arantools/index.json`.

> Note: the auto-created `github-pages` environment only allows deploys from
> `main`, which is why the workflow triggers on `main` (not on tags).

---

## Publishing a new version (you, each release)

Every push to `main` rebuilds and republishes the feed. To ship an update
clients will actually install, bump the `version` first.

**Shortcut:** bump `version` in `blender_manifest.toml`, then double-click
`publish.bat` (or run `publish.bat "my message"`). It commits and pushes
`main`, which triggers the build + deploy.

Manual equivalent:

```
git commit -am "v0.2.1"
git push origin main
```

No tags needed — the `github-pages` environment only allows deploys from
`main`, and the extension version comes from the manifest, not from tags.

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
