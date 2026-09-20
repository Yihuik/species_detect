[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Contact,
    [string]$ProjectRoot = "",
    [string]$Model = "qwen3-vl-plus"
)

$ErrorActionPreference = "Stop"
if (-not $ProjectRoot) {
    $ProjectRoot = Split-Path -Parent $PSCommandPath
}
$root = (Resolve-Path -LiteralPath $ProjectRoot).Path
$envFile = Join-Path $root ".env"
if (-not (Test-Path -LiteralPath $envFile)) {
    throw "Missing .env file: $envFile"
}

Get-Content -LiteralPath $envFile | ForEach-Object {
    $line = $_.Trim()
    if (-not $line -or $line.StartsWith("#")) { return }
    if ($line -notmatch '^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
        throw "Invalid .env entry: $line"
    }
    $name = $Matches[1]
    $value = $Matches[2].Trim()
    if ($value.Length -ge 2 -and (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'")))) {
        $value = $value.Substring(1, $value.Length - 2)
    }
    [Environment]::SetEnvironmentVariable($name, $value, "Process")
}

if (-not $env:DASHSCOPE_API_KEY) {
    throw "DASHSCOPE_API_KEY is not set after loading .env"
}

$env:PYTHONPATH = Join-Path $root "src"
$runStamp = Get-Date -Format "yyyyMMdd-HHmmss"
$runDir = Join-Path $root "runs/agent-$runStamp"

Push-Location $root
try {
    python -m agentized_workflow.cli photos sync --project-root $root
    if ($LASTEXITCODE -ne 0) { throw "Catalog sync failed with exit code $LASTEXITCODE" }

    python -m agentized_workflow.cli photos collect --project-root $root --contact $Contact
    if ($LASTEXITCODE -ne 0) { throw "Public-source collection failed with exit code $LASTEXITCODE" }

    python -m agentized_workflow.cli `
        --input-dir (Join-Path $root "photos") `
        --metadata-csv (Join-Path $root "photos/workflow_metadata.csv") `
        --live `
        --model $Model `
        --run-dir $runDir
    if ($LASTEXITCODE -ne 0) { throw "Automatic labeling failed with exit code $LASTEXITCODE" }
}
finally {
    Pop-Location
}
