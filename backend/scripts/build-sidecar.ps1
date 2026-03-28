param(
  [string]$TargetName = "open-eagle-agent",
  [string]$TargetTriple = "x86_64-pc-windows-msvc"
)

$ErrorActionPreference = "Stop"

$projectRoot = Resolve-Path (Join-Path $PSScriptRoot "..\\..")
$backendRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$binaryRoot = Join-Path $projectRoot "src-tauri\\binaries"

if (!(Test-Path $binaryRoot)) {
  New-Item -ItemType Directory -Path $binaryRoot | Out-Null
}

uv sync --project $backendRoot --extra build
uv run --project $backendRoot pyinstaller `
  --noconfirm `
  --onefile `
  --name "$TargetName-$TargetTriple" `
  --distpath $binaryRoot `
  (Join-Path $backendRoot "app\\main.py")
