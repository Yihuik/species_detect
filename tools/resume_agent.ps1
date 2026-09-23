[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$RunDir,
    [string]$ProjectRoot = "",
    [string]$Model = "qwen3-vl-plus"
)

$ErrorActionPreference = "Stop"
if (-not $ProjectRoot) {
    $ProjectRoot = Split-Path -Parent $PSScriptRoot
}
$root = (Resolve-Path -LiteralPath $ProjectRoot).Path
$run = (Resolve-Path -LiteralPath $RunDir).Path
if (-not $run.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "RunDir must remain under the project root: $root"
}

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
if (-not $env:DASHSCOPE_BASE_URL) {
    throw "DASHSCOPE_BASE_URL is not set after loading .env"
}

$env:PYTHONPATH = Join-Path $root "src"
$env:PYTHONUTF8 = "1"
Push-Location $root
try {
    python -m agentized_workflow.cli `
        --input-dir (Join-Path $root "photos") `
        --metadata-csv (Join-Path $root "photos/workflow_metadata.csv") `
        --live `
        --model $Model `
        --base-url $env:DASHSCOPE_BASE_URL `
        --run-dir $run
    if ($LASTEXITCODE -ne 0) { throw "Resumed automatic labeling failed with exit code $LASTEXITCODE" }
}
finally {
    Pop-Location
}
