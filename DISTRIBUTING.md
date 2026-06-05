# Auto-updating Aran Tools via GitHub (no separate host)

Blender 4.2+ installs this add-on as an **extension** and checks a repository
URL for updates on startup. We serve that repository for free from **GitHub
Pages** — a GitHub Action rebuilds the zip and the `index.json` every time you
push a version tag. Your teammate adds one URL once; after that updates are
automatic.

Pieces already in this folder:
- `blender_manifest.toml` — marks it as an extension.
- `.github/workflows/release.yml` — builds & publishes to Pages.
- `publish.bat` — one-click commit + push + release tag.

---

## One-time setup (you)

1. **Create a GitHub repo** and push this folder so that
   `blender_manifest.toml` sits at the **repo root**.

   ```
   git init
   git add .
   git commit -m "Aran Tools extension"
   git branch -M main
   git remote add origin https://github.com/<you>/<repo>.git
   git push -u origin main
   ```

2. **Enable Pages via Actions**: repo **Settings ▸ Pages ▸ Build and
   deployment ▸ Source = GitHub Actions**.

3. No secrets/tokens needed — the workflow uses the repo's built-in Pages
   permissions.

---

## Publishing a version (you, each release)

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

When the Action finishes, your feed lives at:

```
https://<you>.github.io/<repo>/index.json
```

---

## Installing (your teammate, once)

1. **Edit ▸ Preferences ▸ Get Extensions**.
2. Top-right dropdown (⌄) ▸ **Add Remote Repository**.
3. Paste the URL: `https://<you>.github.io/<repo>/index.json`
4. Tick **Check for Updates on Startup**, confirm.
5. Search "Aran Tools" in Get Extensions ▸ **Install**.

From then on, every new tag you push is offered as an update on their next
launch (or via **Check for Updates**).

---

## Notes / caveats

- **Never change `id`** in the manifest once shared — the updater keys off it.
- The Action pins Blender `4.2.3` just to *build* the package (any 4.2+
  works). If that download URL 404s, bump `BLENDER_VERSION` / `BLENDER_SERIES`
  at the top of the workflow.
- A **private repo**'s Pages needs a paid plan; for free use make the repo
  public (the add-on is GPL anyway) or use a feed the teammate can reach.
- The panel's **Reload Addon** button reloads the `arantools` package by name;
  installed as an extension the package is renamed (`bl_ext.<repo>.arantools`)
  so that dev button may not work for teammates — they should use Blender's
  normal enable/disable or the extension updater.
