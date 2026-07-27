# Build the standalone gsg.exe with PyInstaller.
# Run from the repo root: powershell -File packaging\build_exe.ps1
# Output: dist\gsg.exe (single file, no Python required on the target machine).

$ErrorActionPreference = "Stop"

$python = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "python" }

& $python -m pip install --upgrade pyinstaller | Out-Null

$icon = "assets\icon.ico"
if (-not (Test-Path $icon)) {
    throw "Missing $icon - run: python packaging\make_icon.py"
}

# The tray glyphs are read at runtime, so they must travel inside the exe.
# Bundling them under the package directory means tray.asset_path finds them
# the same way in a bundle as from source (__file__-relative).
& $python -m PyInstaller `
    --onefile `
    --console `
    --clean `
    --name gsg `
    --icon $icon `
    --add-data "src\game_save_genie\assets\tray;game_save_genie/assets/tray" `
    --distpath dist `
    --workpath build `
    --specpath build `
    packaging\gsg_entry.py

if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed" }

Write-Output ""
Write-Output "Built dist\gsg.exe"
& ".\dist\gsg.exe" --version
