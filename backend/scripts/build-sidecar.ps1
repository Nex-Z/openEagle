param(
  [string]$TargetName = "open-eagle-agent"
)

$ErrorActionPreference = "Stop"

$projectRoot = Resolve-Path (Join-Path $PSScriptRoot "..\\..")
$env:OPEN_EAGLE_SIDECAR_NAME = $TargetName
node (Join-Path $projectRoot "scripts\\build-sidecar.cjs")
