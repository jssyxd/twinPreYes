$ErrorActionPreference = 'Continue'
$dir   = 'C:\Users\da\my155-inspect'
$vibe  = 'C:\Users\da\AppData\Local\Programs\VibeShell CLI\vibeshell.exe'
$herdr = 'C:\Users\da\AppData\Local\Programs\Herdr\bin\herdr.exe'
$env:HERDR_ENV = '1'
$log   = Join-Path $dir 'trigger.log'
$stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'

# ---- 1) pull dual-track ledger from my155 (UTF-8), retry on transient SSH failure ----
$report = Join-Path $dir 'last_report.txt'
$body = ''
for ($i = 1; $i -le 3; $i++) {
    $body = & $vibe ssh malaysia --command-file (Join-Path $dir 'pull.sh') 2>&1 | Out-String
    if ($body -match '===== (PAPER|LIVE)') { break }
    Add-Content -Path $log -Value ($stamp + " | pull attempt " + $i + " failed, retrying in 20s")
    Start-Sleep -Seconds 20
}
$sshOk = ($body -match '===== (PAPER|LIVE)')
$body += "--- pull done " + $stamp + " (attempts ok=" + $sshOk + ") ---`r`n"
[System.IO.File]::WriteAllText($report, $body, (New-Object System.Text.UTF8Encoding($false)))

# ---- 2) change detection (ignore per-cycle timestamps) ----
$norm = ($body -replace '20\d\d-\d\d-\d\dT[0-9:\.]+Z', '') -replace '--- pull done[^\r\n]*', ''
$norm = [regex]::Replace($norm, '\d+\.\d{3,}', { param($m) ([double]$m.Value).ToString('0.00') })
$sha = [System.Security.Cryptography.SHA256]::Create()
$hash = ([BitConverter]::ToString($sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($norm)))).Replace('-','').Substring(0,12)
$hashFile = Join-Path $dir 'last_hash.txt'
$prev = if (Test-Path $hashFile) { (Get-Content $hashFile -Raw).Trim() } else { '' }
$changed = ($prev -ne $hash)
[System.IO.File]::WriteAllText($hashFile, $hash, (New-Object System.Text.UTF8Encoding($false)))

# ---- 2b) quiet-cycle suppression: on change always wake; when quiet, heartbeat every 4th cycle (~2h) ----
$streakFile = Join-Path $dir 'change_streak.txt'
$streak = if (Test-Path $streakFile) { [int](Get-Content $streakFile -Raw).Trim() } else { 0 }
if ($changed) { $streak = 0 } else { $streak = $streak + 1 }
[System.IO.File]::WriteAllText($streakFile, "$streak", (New-Object System.Text.UTF8Encoding($false)))
$inject = $changed -or (($streak % 4) -eq 0)
if (-not $inject) {
    Add-Content -Path $log -Value ($stamp + " | report written, inject skipped (quiet streak=" + $streak + ")")
    exit 0
}

# ---- 3) resolve target pane dynamically (agent name -> pane id) ----
$target = $null
try {
    $raw = (& $herdr agent list 2>$null | Out-String)
    $agents = ($raw | ConvertFrom-Json).result.agents
    $hit = $agents | Where-Object { $_.agent -eq 'omp' -and $_.cwd -like 'W:\20260907*' } | Select-Object -First 1
    if (-not $hit) { $hit = $agents | Where-Object { $_.agent -eq 'omp' } | Select-Object -First 1 }
    if (-not $hit) { $hit = $agents | Select-Object -First 1 }
    if ($hit) { $target = $hit.pane_id }
} catch { }

if (-not $target) { Add-Content -Path $log -Value ($stamp + " | ERROR: no agent pane found (changed=" + $changed + ")"); exit 1 }

# ---- 4) submit inspection prompt (fallback: send-text + Enter) ----
$prompt = (Get-Content (Join-Path $dir 'prompt.txt') -Raw -Encoding UTF8).Trim()
$prompt = $prompt.Replace('{{CHANGED}}', $changed.ToString()).Replace('{{HASH}}', $hash)
if (-not $sshOk) { $prompt = "[SSH_FAIL] 上轮拉取失败: " + $prompt }
$out = & $herdr agent prompt $target $prompt 2>&1
if ($out -match 'error') {
    Add-Content -Path $log -Value ($stamp + " | prompt->" + $target + " FAILED changed=" + $changed + " sshOk=" + $sshOk + ": " + ($out -join ' ') + " | fallback")
    $null = & $herdr pane send-text $target $prompt 2>&1
    $null = & $herdr pane send-keys $target Enter 2>&1
} else {
    Add-Content -Path $log -Value ($stamp + " | prompt->" + $target + " OK changed=" + $changed + " sshOk=" + $sshOk + " hash=" + $hash)
}
