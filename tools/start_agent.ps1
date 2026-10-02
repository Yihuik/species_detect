[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Contact,
    [string]$ProjectRoot = "",
    [string]$Model = "qwen3-vl-plus",
    [double]$Threshold = 0.7,
    [switch]$RetryRejected,
    [int]$RetryCaseId = 0,
    [switch]$AllowNewProfile
)

$ErrorActionPreference = "Stop"
if (-not $ProjectRoot) {
    $ProjectRoot = Split-Path -Parent $PSScriptRoot
}
$root = (Resolve-Path -LiteralPath $ProjectRoot).Path
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
$runsRoot = Join-Path $root "runs"
New-Item -ItemType Directory -Path $runsRoot -Force | Out-Null
try {
    $launcherLock = [System.IO.File]::Open(
        (Join-Path $runsRoot "label-launch.lock"),
        [System.IO.FileMode]::OpenOrCreate,
        [System.IO.FileAccess]::ReadWrite,
        [System.IO.FileShare]::None
    )
}
catch [System.IO.IOException] {
    throw "another labeling launcher is already running; check its process before retrying"
}
Push-Location $root
try {
    python -m agentized_workflow.cli photos sync --project-root $root
    if ($LASTEXITCODE -ne 0) { throw "Catalog sync failed with exit code $LASTEXITCODE" }

    python -m agentized_workflow.cli photos collect --project-root $root --contact $Contact --allow-partial
    if ($LASTEXITCODE -ne 0) { throw "Public-source collection failed with exit code $LASTEXITCODE" }

    python -m agentized_workflow.cli photos ingest --project-root $root
    if ($LASTEXITCODE -ne 0) { throw "User photo ingestion failed with exit code $LASTEXITCODE" }

    $planArgs = @(
        "-m", "agentized_workflow.cli", "photos", "labels", "plan",
        "--photos-root", (Join-Path $root "photos"),
        "--metadata-csv", (Join-Path $root "photos/workflow_metadata.csv"),
        "--model", $Model, "--base-url", $env:DASHSCOPE_BASE_URL,
        "--threshold", [string]$Threshold
    )
    if ($RetryRejected) { $planArgs += "--retry-rejected" }
    if ($RetryCaseId -gt 0) { $planArgs += @("--retry-case-id", [string]$RetryCaseId) }
    $planText = python @planArgs
    if ($LASTEXITCODE -ne 0) { throw "Label planning failed with exit code $LASTEXITCODE" }
    $plan = $planText | ConvertFrom-Json
    if ($plan.profile_changed -gt 0 -and -not $AllowNewProfile) {
        throw "Labeling profile changed for $($plan.profile_changed) photos; review the model/policy and use -AllowNewProfile to authorize relabeling"
    }
    foreach ($existingRun in @($plan.resume_runs)) {
        python -m agentized_workflow.cli `
            --resume-existing `
            --live `
            --model $Model `
            --base-url $env:DASHSCOPE_BASE_URL `
            --threshold $Threshold `
            --label-registry (Join-Path $root "photos") `
            --run-dir $existingRun
        if ($LASTEXITCODE -ne 0) { throw "Resume failed for $existingRun with exit code $LASTEXITCODE" }
        python -m agentized_workflow.render_labels `
            --photos-root (Join-Path $root "photos") --run-dir $existingRun
        if ($LASTEXITCODE -ne 0) { throw "Rendering failed for $existingRun with exit code $LASTEXITCODE" }
    }
    if (@($plan.resume_runs).Count -gt 0) {
        $planText = python @planArgs
        if ($LASTEXITCODE -ne 0) { throw "Label planning failed after resume" }
        $plan = $planText | ConvertFrom-Json
        if ($plan.profile_changed -gt 0 -and -not $AllowNewProfile) {
            throw "Labeling profile changed for $($plan.profile_changed) photos; use -AllowNewProfile after review"
        }
    }
    if ($plan.pending -eq 0) {
        python -m agentized_workflow.cli photos labels render-missing `
            --photos-root (Join-Path $root "photos")
        if ($LASTEXITCODE -ne 0) { throw "Existing label rendering failed with exit code $LASTEXITCODE" }
        Write-Output ("No new model labeling needed: " + $planText)
        return
    }
    $runStamp = Get-Date -Format "yyyyMMdd-HHmmss-fff"
    $runDir = Join-Path $root "runs/agent-$runStamp"
    $labelArgs = @(
        "-m", "agentized_workflow.cli",
        "--input-dir", (Join-Path $root "photos"),
        "--metadata-csv", (Join-Path $root "photos/workflow_metadata.csv"),
        "--live", "--model", $Model,
        "--base-url", $env:DASHSCOPE_BASE_URL,
        "--threshold", [string]$Threshold,
        "--label-registry", (Join-Path $root "photos"),
        "--run-dir", $runDir
    )
    if ($RetryRejected) { $labelArgs += "--retry-rejected" }
    if ($RetryCaseId -gt 0) { $labelArgs += @("--retry-case-id", [string]$RetryCaseId) }
    if ($AllowNewProfile) { $labelArgs += "--allow-new-profile" }
    python @labelArgs
    if ($LASTEXITCODE -ne 0) { throw "Automatic labeling failed with exit code $LASTEXITCODE" }

    python -m agentized_workflow.cli photos labels render-missing `
        --photos-root (Join-Path $root "photos")
    if ($LASTEXITCODE -ne 0) { throw "Label rendering failed with exit code $LASTEXITCODE" }
}
finally {
    try { Pop-Location }
    finally { $launcherLock.Dispose() }
}
