# build_exe_universal.ps1 - portable, no personal info
param(
    [string]$ProjectDir = ""
)

$ErrorActionPreference = "Stop"

# Resolve project directory: param -> script folder -> current folder
if (-not $ProjectDir) {
    if ($PSScriptRoot) { $ProjectDir = $PSScriptRoot }
    else { $ProjectDir = (Get-Location).Path }
}
$ProjectDir = (Resolve-Path -Path $ProjectDir).Path

$ScriptPath = Join-Path $ProjectDir "run_claude_local_universal.py"
$IconPath   = Join-Path $ProjectDir "app.ico"
$DistDir    = Join-Path $ProjectDir "dist-universal"
$BuildDir   = Join-Path $ProjectDir "build-universal"

Write-Host "==> Project directory: $ProjectDir"

if (-not (Test-Path $ScriptPath)) {
    throw "run_claude_local_universal.py not found in $ProjectDir. Put this script next to the .py, or pass -ProjectDir <folder>."
}

Write-Host "==> Verifying PyInstaller is importable..."
$pyiVersion = python -m PyInstaller --version
if (-not $pyiVersion) {
    throw "PyInstaller not importable. Run: python -m pip install pyinstaller"
}
Write-Host "    PyInstaller $pyiVersion"

Write-Host "==> Installing/refreshing runtime dependencies..."
python -m pip install --upgrade pystray pillow ddgs pypdf

if (-not (Test-Path $IconPath)) {
    Write-Host "==> Generating app.ico..."
    # Use a temp .py to avoid quoting issues with apostrophes in the path
    $iconGen = Join-Path $env:TEMP "gen_icon_$(Get-Random).py"
    @"
from PIL import Image, ImageDraw
img = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.ellipse([8, 8, 248, 248], fill=(99, 102, 241, 255), outline=(255, 255, 255, 255), width=8)
d.arc([64, 48, 192, 208], start=45, end=315, fill=(255, 255, 255, 255), width=20)
img.save(r"$IconPath", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
"@ | Set-Content -Path $iconGen -Encoding UTF8
    python $iconGen
    Remove-Item $iconGen -ErrorAction SilentlyContinue
} else {
    Write-Host "==> app.ico already exists, reusing."
}

Write-Host "==> Building exe with PyInstaller..."
python -m PyInstaller `
    --noconfirm `
    --clean `
    --onedir `
    --windowed `
    --name "ClaudeLocalProxy" `
    --icon "$IconPath" `
    --hidden-import "pystray._win32" `
    --collect-submodules "pystray" `
    --collect-submodules "ddgs" `
    --exclude-module "tkinter" `
    --distpath "$DistDir" `
    --workpath "$BuildDir" `
    --specpath "$BuildDir" `
    "$ScriptPath"

$exePath = Join-Path $DistDir "ClaudeLocalProxy\ClaudeLocalProxy.exe"
if (Test-Path $exePath) {
    Write-Host ""
    Write-Host "==> DONE: $exePath" -ForegroundColor Green
    Write-Host "    Ship the whole '$DistDir\ClaudeLocalProxy' folder + README.md."
} else {
    Write-Host "==> Build failed - check output above." -ForegroundColor Red
}