# Updates one GitHub revision. Never downloads or replaces .env or model weights.
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
if (!(Test-Path -LiteralPath (Join-Path $root 'compose.yaml'))) {
    throw 'Put update_bot.ps1 in the bot folder next to compose.yaml.'
}
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$stage = Join-Path ([IO.Path]::GetTempPath()) ('pocket-update-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $stage | Out-Null
$backup = Join-Path $root ('.bot-update-backups\' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
$changed = @()
$stopped = $false
Push-Location $root
try {
    $headers = @{ 'User-Agent' = 'PocketSignalBot-Updater'; 'Accept' = 'application/vnd.github+json' }
    $info = Invoke-RestMethod -Uri 'https://api.github.com/repos/frixsis9-jpg/AI/commits/main' -Headers $headers -TimeoutSec 30
    $revision = [string]$info.sha
    if ($revision -notmatch '^[a-f0-9]{40}$') { throw 'Invalid GitHub revision.' }
    $base = 'https://raw.githubusercontent.com/frixsis9-jpg/AI/' + $revision + '/'
    $manifestPath = Join-Path $stage 'UPDATE_MANIFEST.json'
    Invoke-WebRequest -UseBasicParsing -Uri ($base + 'UPDATE_MANIFEST.json') -OutFile $manifestPath -TimeoutSec 30
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($manifest.schema -ne 1 -or $manifest.files.Count -lt 1 -or $manifest.files.Count -gt 100) {
        throw 'Unsupported or empty update manifest.'
    }
    $seen = @{}
    foreach ($file in $manifest.files) {
        $relative = [string]$file.path
        if ($relative -notmatch '^[A-Za-z0-9_./-]+$' -or $relative.Contains('..') -or $relative.StartsWith('/') -or
            $relative -match '(^|/)(\.env[^/]*|\.git|model_weights\.json)$' -or $relative -match '\.(db|exe)$') {
            throw ('Protected or invalid update path: ' + $relative)
        }
        if ($seen.ContainsKey($relative)) { throw 'Duplicate update path.' }
        $seen[$relative] = $true
        $target = Join-Path $stage $relative
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
        Invoke-WebRequest -UseBasicParsing -Uri ($base + $relative) -OutFile $target -TimeoutSec 30
        $actual = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actual -ne ([string]$file.sha256).ToLowerInvariant()) { throw ('SHA-256 mismatch: ' + $relative) }
    }
    & docker compose stop bot
    if ($LASTEXITCODE -ne 0) { throw 'Could not stop Docker container.' }
    $stopped = $true
    New-Item -ItemType Directory -Force -Path $backup | Out-Null
    foreach ($db in @('signals.db', 'docker-data/signals.db')) {
        $dbPath = Join-Path $root $db
        foreach ($suffix in @('', '-wal', '-shm', '-journal')) {
            if (Test-Path -LiteralPath ($dbPath + $suffix) -PathType Leaf) {
                $copy = Join-Path $backup ($db + $suffix)
                New-Item -ItemType Directory -Force -Path (Split-Path -Parent $copy) | Out-Null
                Copy-Item -LiteralPath ($dbPath + $suffix) -Destination $copy
            }
        }
    }
    foreach ($file in $manifest.files) {
        $relative = [string]$file.path
        $destination = Join-Path $root $relative
        $existed = Test-Path -LiteralPath $destination -PathType Leaf
        if ($existed) {
            $copy = Join-Path $backup $relative
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $copy) | Out-Null
            Copy-Item -LiteralPath $destination -Destination $copy
        }
        $changed += @{ Path = $relative; Existed = $existed }
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destination) | Out-Null
        Copy-Item -LiteralPath (Join-Path $stage $relative) -Destination $destination -Force
    }
    & docker compose up -d --build bot
    if ($LASTEXITCODE -ne 0) { throw 'Docker build/start failed.' }
    Write-Host ('Updated revision: ' + $revision)
    Write-Host ('Backup: ' + $backup)
    Write-Host 'Open Telegram, choose expiry and Manual/Auto pair, then Start search.'
    Write-Host 'Check startup logs: docker compose logs --since 2m bot'
} catch {
    if ($stopped) {
        & docker compose stop bot
        foreach ($item in $changed) {
            $destination = Join-Path $root $item.Path
            if ($item.Existed) {
                Copy-Item -LiteralPath (Join-Path $backup $item.Path) -Destination $destination -Force
            } elseif (Test-Path -LiteralPath $destination -PathType Leaf) {
                Remove-Item -LiteralPath $destination -Force
            }
        }
        Write-Host 'Source files restored. Data preserved. Container left stopped; inspect the error below.'
    }
    throw
} finally {
    Pop-Location
    if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
}
