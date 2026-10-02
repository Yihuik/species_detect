[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$RunDir,
    [string]$ProjectRoot = "",
    [string]$Model = "qwen3-vl-plus",
    [double]$Threshold = 0.7,
    [int]$MaxTargets = 10
)

$ErrorActionPreference = "Stop"
if (-not $ProjectRoot) {
    $ProjectRoot = Split-Path -Parent $PSScriptRoot
}
$root = (Resolve-Path -LiteralPath $ProjectRoot).Path
$run = (Resolve-Path -LiteralPath $RunDir).Path
$runsRoot = (Join-Path $root "runs").TrimEnd([char]92) + [char]92
if (-not $run.StartsWith($runsRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "RunDir must be under $runsRoot"
}
$envFile = Join-Path $root ".env"
if (-not (Test-Path -LiteralPath $envFile)) {
    throw "Missing .env file: $envFile"
}
$baseUrl = ""
Get-Content -LiteralPath $envFile -Encoding UTF8 | ForEach-Object {
    $line = $_.Trim()
    if ($line -match '^(?:export\s+)?DASHSCOPE_BASE_URL=(.*)$') {
        $baseUrl = $Matches[1].Trim()
        if ($baseUrl.Length -ge 2 -and
            (($baseUrl.StartsWith('"') -and $baseUrl.EndsWith('"')) -or
             ($baseUrl.StartsWith("'") -and $baseUrl.EndsWith("'")))) {
            $baseUrl = $baseUrl.Substring(1, $baseUrl.Length - 2)
        }
    }
}
if (-not $baseUrl) {
    throw "DASHSCOPE_BASE_URL is not set in .env"
}
$env:PYTHONPATH = Join-Path $root "src"
$env:PYTHONUTF8 = "1"
$libraryDb = Join-Path $root "photos/photo_library.sqlite3"
if (-not (Test-Path -LiteralPath $libraryDb)) {
    throw "Photo library database is missing: $libraryDb"
}
try {
    $launcherLock = [System.IO.File]::Open(
        (Join-Path $root "runs/label-launch.lock"),
        [System.IO.FileMode]::OpenOrCreate,
        [System.IO.FileAccess]::ReadWrite,
        [System.IO.FileShare]::None
    )
}
catch [System.IO.IOException] {
    throw "another labeling launcher is already running; import only after it exits"
}
Push-Location $root
try {
    $backupRoot = Join-Path $root "runs/backups"
    New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
    $backupName = "photo_library-" + (Get-Date -Format "yyyyMMdd-HHmmss-fff") + "-" +
        [guid]::NewGuid().ToString("N").Substring(0, 8) + ".sqlite3"
    $backupPath = Join-Path $backupRoot $backupName
    python -c "import sqlite3,sys; source=sqlite3.connect(sys.argv[1]); target=sqlite3.connect(sys.argv[2]); source.backup(target); target.close(); source.close()" `
        $libraryDb $backupPath
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $backupPath)) {
        throw "Photo library SQLite online backup failed"
    }
    python -m agentized_workflow.cli photos labels import-run `
        --photos-root (Join-Path $root "photos") `
        --run-dir $run `
        --model $Model `
        --base-url $baseUrl `
        --threshold $Threshold `
        --max-targets $MaxTargets
    if ($LASTEXITCODE -ne 0) { throw "Historical label import failed with exit code $LASTEXITCODE" }
    python -m agentized_workflow.render_labels `
        --photos-root (Join-Path $root "photos") --run-dir $run
    if ($LASTEXITCODE -ne 0) { throw "Historical result rendering failed with exit code $LASTEXITCODE" }
}
finally {
    try { Pop-Location }
    finally { $launcherLock.Dispose() }
}
