param(
  [string]$TargetName = "open-eagle-agent"
)

$ErrorActionPreference = "Stop"

$projectRoot = Resolve-Path (Join-Path $PSScriptRoot "..\\..")
$backendRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$binaryRoot = Join-Path $backendRoot "binaries"

if (!(Test-Path $binaryRoot)) {
  New-Item -ItemType Directory -Path $binaryRoot | Out-Null
}

uv sync --project $backendRoot --extra build
uv run --project $backendRoot pyinstaller `
  --noconfirm `
  --onefile `
  --name "$TargetName" `
  --distpath $binaryRoot `
  (Join-Path $backendRoot "app\\main.py")
