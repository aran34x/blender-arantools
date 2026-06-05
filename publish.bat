@echo off
REM ============================================================================
REM  Aran Tools - commit and push to GitHub, which republishes the extension
REM ----------------------------------------------------------------------------
REM  Usage:   publish.bat "your commit message"
REM           (message is optional; defaults to "Update")
REM
REM  What it does:
REM    Commits all changes and pushes them to 'main'. Every push to main runs
REM    the "Build & Publish Extension" Action, which rebuilds the zip + index
REM    and deploys to GitHub Pages. Blender clients see the new version on
REM    their next "Check for Updates".
REM
REM  To ship an UPDATE that clients will actually install, bump `version` in
REM  blender_manifest.toml before running this (otherwise the feed just
REM  refreshes the same version).
REM
REM  One-time prerequisites (see DISTRIBUTING.md):
REM    - git installed and this folder is a git repo with an 'origin' remote
REM    - repo is public, Settings > Pages > Source = GitHub Actions
REM ============================================================================

setlocal
cd /d "%~dp0"

set "MSG=%~1"
if "%MSG%"=="" set "MSG=Update"

echo.
echo === Committing and pushing main ===
git add -A
git commit -m "%MSG%" || echo (nothing to commit - pushing existing commits)
git push origin main
if errorlevel 1 (
    echo.
    echo ERROR: 'git push origin main' failed. Is 'origin' set and are you logged in?
) else (
    echo.
    echo Pushed. The GitHub Action will rebuild and republish the feed.
    echo Watch it under the repo's Actions tab.
)

echo.
pause
endlocal
