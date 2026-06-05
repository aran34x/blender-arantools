@echo off
REM ============================================================================
REM  Aran Tools - push to GitHub (and publish a release when version is bumped)
REM ----------------------------------------------------------------------------
REM  Usage:   publish.bat "your commit message"
REM           (message is optional; defaults to "Update")
REM
REM  What it does:
REM    1. Commits all changes and pushes them to the 'main' branch.
REM    2. Reads `version` from blender_manifest.toml.
REM    3. If a tag v<version> doesn't exist yet, creates and pushes it -
REM       which triggers the GitHub Action that rebuilds the extension and
REM       publishes it to GitHub Pages (so teammates get the auto-update).
REM
REM  One-time prerequisites (see DISTRIBUTING.md):
REM    - git installed and this folder is a git repo with an 'origin' remote
REM    - Settings > Pages > Source = GitHub Actions
REM ============================================================================

setlocal enabledelayedexpansion
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
    goto :end
)

REM --- Read version from blender_manifest.toml -------------------------------
set "VER="
for /f "tokens=2 delims==" %%A in ('findstr /b /c:"version" blender_manifest.toml') do set "VER=%%A"
set "VER=!VER: =!"
set "VER=!VER:"=!"

if "!VER!"=="" (
    echo.
    echo WARNING: could not read version from blender_manifest.toml - skipping release tag.
    goto :end
)

set "TAG=v!VER!"
echo.
echo === Release tag: !TAG! ===
git rev-parse "!TAG!" >nul 2>&1
if errorlevel 1 (
    echo Creating and pushing !TAG! ...
    git tag "!TAG!"
    git push origin "!TAG!"
    if errorlevel 1 (
        echo ERROR: failed to push tag !TAG!.
    ) else (
        echo Done - the GitHub Action will build and publish the update.
    )
) else (
    echo Tag !TAG! already exists - no new release.
    echo Bump "version" in blender_manifest.toml to publish a new update.
)

:end
echo.
pause
endlocal
