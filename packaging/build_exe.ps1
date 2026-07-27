# Build the standalone gsg.exe with PyInstaller.
# Run from the repo root: powershell -File packaging\build_exe.ps1
# Output: dist\gsg.exe (single file, no Python required on the target machine).

$ErrorActionPreference = "Stop"

$python = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "python" }

& $python -m pip install --upgrade pyinstaller | Out-Null

if (-not (Test-Path "assets\icon.ico")) {
    throw "Missing assets\icon.ico - run: python packaging\make_icon.py"
}
if (-not (Test-Path "src\game_save_genie\assets\tray")) {
    throw "Missing tray glyphs - run: python packaging\make_icon.py"
}

# Absolute paths on purpose. --specpath puts the generated spec in build\, and
# PyInstaller resolves relative --icon / --add-data sources against the SPEC
# directory rather than the working directory, so relative paths here silently
# become build\assets\... and the build fails (or worse, drops the data).
$icon = (Resolve-Path "assets\icon.ico").Path
$trayAssets = (Resolve-Path "src\game_save_genie\assets\tray").Path

# The tray glyphs are read at runtime, so they must travel inside the exe.
# Bundling them under the package directory means tray.asset_path finds them
# the same way in a bundle as from source (__file__-relative).
& $python -m PyInstaller `
    --onefile `
    --console `
    --clean `
    --name gsg `
    --icon $icon `
    --add-data "$trayAssets;game_save_genie/assets/tray" `
    --distpath dist `
    --workpath build `
    --specpath build `
    packaging\gsg_entry.py

if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed" }

# Verify the runtime assets actually made it in. A build that silently drops
# them produces an exe whose tray icon can never appear: create_tray finds no
# glyphs, returns NullTray, and the daemon runs with no visible status at all.
$toc = Get-ChildItem build -Recurse -Filter "*.toc" -ErrorAction SilentlyContinue
$bundledGlyphs = $toc | Select-String -Pattern "ok\.png|warn\.png|error\.png|paused\.png"
if (-not $bundledGlyphs) {
    throw "Tray glyphs are missing from the bundle - the tray icon would never appear."
}

Write-Output ""
Write-Output "Built dist\gsg.exe"
& ".\dist\gsg.exe" --version
