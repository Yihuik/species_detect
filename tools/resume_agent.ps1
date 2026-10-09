[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$RunDir,
    [string]$ProjectRoot = "",
    [string]$Model = "qwen3-vl-plus",
    [Nullable[double]]$Threshold = $null
)

$ErrorActionPreference = "Stop"
if (-not $ProjectRoot) {
    $ProjectRoot = Split-Path -Parent $PSScriptRoot
}
$root = (Resolve-Path -LiteralPath $ProjectRoot).Path
$run = (Resolve-Path -LiteralPath $RunDir).Path
$runsRoot = (Join-Path $root "runs").TrimEnd('\') + '\'
if (-not $run.StartsWith($runsRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "RunDir must remain under the runs directory: $runsRoot"
}

$envFile = Join-Path $root ".env"
if (-not (Test-Path -LiteralPath $envFile)) {
    throw "Missing .env file: $envFile"
}
Get-Content -LiteralPath $envFile -Encoding UTF8 | ForEach-Object {
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
try {
    $launcherLock = [System.IO.File]::Open(
        (Join-Path $root "runs/label-launch.lock"),
        [System.IO.FileMode]::OpenOrCreate,
        [System.IO.FileAccess]::ReadWrite,
        [System.IO.FileShare]::None
    )
}
catch [System.IO.IOException] {
    throw "another labeling launcher is already running; check its process before retrying"
}
$policyArgs = @()
if ($PSBoundParameters.ContainsKey("Threshold")) { $policyArgs += @("--threshold", [string]$Threshold) }
Push-Location $root
try {
    python -m agentized_workflow.cli `
        --resume-existing `
        --live `
        --model $Model `
        --base-url $env:DASHSCOPE_BASE_URL `
        @policyArgs `
        --label-registry (Join-Path $root "photos") `
        --run-dir $run
    if ($LASTEXITCODE -ne 0) { throw "Resumed automatic labeling failed with exit code $LASTEXITCODE" }
    python -m agentized_workflow.render_labels `
        --photos-root (Join-Path $root "photos") --run-dir $run
    if ($LASTEXITCODE -ne 0) { throw "Rendering failed with exit code $LASTEXITCODE" }
}
finally {
    try { Pop-Location }
    finally { $launcherLock.Dispose() }
}
