$ErrorActionPreference = "Stop"

$DemoDir = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $DemoDir

if (-not (Test-Path ".env") -or -not (Test-Path ".venv")) {
    throw "Run scripts/setup.ps1 first."
}

Get-Content ".env" | ForEach-Object {
    $line = $_.Trim()
    if ($line -ne "" -and -not $line.StartsWith("#")) {
        $parts = $line.Split("=", 2)
        if ($parts.Length -eq 2) {
            if (-not [Environment]::GetEnvironmentVariable($parts[0], "Process")) {
                [Environment]::SetEnvironmentVariable($parts[0], $parts[1], "Process")
            }
        }
    }
}

$Python = Join-Path $DemoDir ".venv\Scripts\python.exe"
$BackendUrl = if ($env:BACKEND_URL) { $env:BACKEND_URL } else { "http://127.0.0.1:8765" }
$BackendHost = if ($env:BACKEND_HOST) { $env:BACKEND_HOST } else { "127.0.0.1" }
$BackendPort = if ($env:BACKEND_PORT) { $env:BACKEND_PORT } else { "8765" }
$Homes = if ($env:HOMES) { $env:HOMES } else { "100" }
$PostPeriod = if ($env:POST_PERIOD) { $env:POST_PERIOD } else { "5" }
$SimSpeed = if ($env:SIM_SPEED) { $env:SIM_SPEED } else { "6" }

Write-Host "Starting signed local backend on $BackendUrl"
$Backend = Start-Process -FilePath $Python -ArgumentList @(
    "-m", "uvicorn", "backend.main:app",
    "--app-dir", $DemoDir,
    "--host", $BackendHost,
    "--port", $BackendPort,
    "--no-access-log"
) -NoNewWindow -PassThru

Start-Sleep -Seconds 1

Write-Host "Starting $Homes homes with HMAC-signed direct-node ingest"
$Simulator = Start-Process -FilePath $Python -ArgumentList @(
    (Join-Path $DemoDir "edge_simulator\simulator.py"),
    "--homes", $Homes,
    "--backend-url", $BackendUrl,
    "--speed", $SimSpeed,
    "--post-period", $PostPeriod,
    "--topology", "direct-node"
) -NoNewWindow -PassThru

Write-Host ""
Write-Host "Open $BackendUrl/"
Write-Host "Press Ctrl-C here to stop local processes."

try {
    Wait-Process -Id @($Backend.Id, $Simulator.Id)
} finally {
    foreach ($proc in @($Simulator, $Backend)) {
        if ($proc -and -not $proc.HasExited) {
            Stop-Process -Id $proc.Id -Force
        }
    }
}
