$ErrorActionPreference = "Stop"

$DemoDir = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $DemoDir

function Find-Python {
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        return @($py.Source, "-3")
    }

    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) {
        return @($python.Source)
    }

    $python3 = Get-Command python3 -ErrorAction SilentlyContinue
    if ($python3) {
        return @($python3.Source)
    }

    throw "Python 3 is required. Install Python, then rerun scripts/setup.ps1."
}

$Python = Find-Python
$PythonExe = $Python[0]
$PythonArgs = @()
if ($Python.Length -gt 1) {
    $PythonArgs = $Python[1..($Python.Length - 1)]
}

if (-not (Test-Path ".venv")) {
    & $PythonExe @PythonArgs -m venv .venv
}

$VenvPython = Join-Path $DemoDir ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    throw "Expected virtualenv Python at $VenvPython"
}

& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install -r requirements.txt

$npm = Get-Command npm -ErrorAction SilentlyContinue
if ($npm) {
    & $npm.Source install
} else {
    Write-Warning "npm was not found; Cloudflare deployment requires Node.js/npm."
}

if (-not (Test-Path ".env")) {
    $Secret = & $VenvPython -c "import secrets; print(secrets.token_urlsafe(32))"
    @(
        "INGEST_AUTH_MODE=hmac",
        "NODE_HMAC_SECRET=$Secret",
        "BACKEND_URL=http://127.0.0.1:8765",
        "",
        "# Local simulator defaults",
        "HOMES=100",
        "POST_PERIOD=5",
        "SIM_SPEED=6",
        "",
        "# Optional AWS demo defaults",
        "AWS_REGION=eu-west-2",
        "HOSTED_HOMES=100",
        "CLOUD_SIMULATOR_ENABLED=false",
        "CLOUD_SIMULATOR_HOME_COUNT=100",
        "CLOUD_SIMULATOR_PERIOD_SECONDS=60",
        "S3_COLD_COPY_ENABLED=false",
        "S3_RETENTION_DAYS=1",
        "LATEST_STATE_TTL_SECONDS=86400",
        "DASHBOARD_SNAPSHOT_MAX_ITEMS=10000",
        "DASHBOARD_CORS_ALLOW_ORIGIN=*",
        "CLOUDFLARE_STREAM_INTERVAL_MS=3000",
        "CLOUDFLARE_SNAPSHOT_CACHE_SECONDS=3"
    ) | Set-Content -Path ".env" -Encoding utf8
} else {
    Write-Host "Keeping existing .env"
}

Write-Host ""
Write-Host "Setup complete."
Write-Host "Run the local signed demo with:"
Write-Host "  powershell -ExecutionPolicy Bypass -File scripts/run_local_demo.ps1"
Write-Host "Validate the Cloudflare Worker bundle with:"
Write-Host "  npm run cloudflare:check"
