param([string]$BuildDirectory = (Join-Path $env:TEMP 'DrMosfet-CodexMonitor-build'))
$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    uv run --python 3.11 --with 'pyinstaller>=6,<7' --with 'bleak>=3,<4' --with 'pyserial>=3.5,<4' python -m PyInstaller --noconfirm --clean --onefile --windowed --name CodexMonitor --paths "$PSScriptRoot/host" --icon "$PSScriptRoot/branding/app.ico" --add-data "$PSScriptRoot/branding/app.ico;branding" --add-data "$PSScriptRoot/branding/app-mark.png;branding" --distpath "$PSScriptRoot/desktop" --workpath $BuildDirectory --specpath $BuildDirectory "$PSScriptRoot/host/desktop.py"
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed' }
} finally { Pop-Location }
