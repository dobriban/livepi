param(
  [string]$GatewayWsUrl = "ws://127.0.0.1:18789/ws",
  [string]$SweepDir = "prompt_injection_lab\results\sweep_email_gmail_local_20260603_163901"
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$resultsRoot = Join-Path $repoRoot "prompt_injection_lab\results"
$resolvedSweepDir = Join-Path $repoRoot $SweepDir
$manifest = "prompt_injection_lab\tasks\generated\email_gemini_extension_500.jsonl"
$envFile = "prompt_injection_lab\.env"
$stopFile = Join-Path $resultsRoot "STOP_email_gemini_500_manual_resume"

$staleStopFiles = @(
  (Join-Path $resultsRoot "STOP_email_gemini_500_continue_20260603_193758"),
  (Join-Path $resolvedSweepDir "STOP"),
  $stopFile
)
foreach ($path in $staleStopFiles) {
  if (Test-Path $path) {
    Remove-Item -LiteralPath $path -Force
  }
}

$listener = Get-NetTCPConnection -LocalPort 18789 -State Listen -ErrorAction SilentlyContinue
if (-not $listener) {
  $gatewayScript = Join-Path $repoRoot ".openclaw-livepi\start-gateway.ps1"
  Start-Process `
    -FilePath "powershell.exe" `
    -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $gatewayScript, "-root", $repoRoot, "-basePath", $env:Path) `
    -WorkingDirectory $repoRoot `
    -WindowStyle Hidden | Out-Null

  for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    $listener = Get-NetTCPConnection -LocalPort 18789 -State Listen -ErrorAction SilentlyContinue
    if ($listener) { break }
  }
  if (-not $listener) {
    throw "OpenClaw gateway did not start on port 18789."
  }
}

$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$stdout = Join-Path $resultsRoot "sweep_email_gemini_500_manual_resume_$stamp.stdout.log"
$stderr = Join-Path $resultsRoot "sweep_email_gemini_500_manual_resume_$stamp.stderr.log"
$processJson = Join-Path $resultsRoot "sweep_email_gemini_500_manual_resume_process_$stamp.json"

$argsList = @(
  "prompt_injection_lab\scripts\sweep_email_gmail_local.py",
  "--resume", $resolvedSweepDir,
  "--email-attack-cases-jsonl", $manifest,
  "--agent", "openclaw",
  "--gateway-ws-url", $GatewayWsUrl,
  "--chat-timeout-s", "420",
  "--case-timeout-s", "900",
  "--env-file", $envFile,
  "--stop-file", $stopFile
)

$process = Start-Process `
  -FilePath (Join-Path $repoRoot ".venv\Scripts\python.exe") `
  -ArgumentList $argsList `
  -WorkingDirectory $repoRoot `
  -RedirectStandardOutput $stdout `
  -RedirectStandardError $stderr `
  -WindowStyle Hidden `
  -PassThru

[pscustomobject]@{
  manifest = $manifest
  stdout = $stdout
  stderr = $stderr
  process_json = $processJson
  started_at = (Get-Date).ToUniversalTime().ToString("o")
  command = @(".venv\Scripts\python.exe") + $argsList
  sweep_dir = $resolvedSweepDir
  pid = $process.Id
  stop_file = $stopFile
  gateway_ws_url = $GatewayWsUrl
} | ConvertTo-Json -Depth 5 | Set-Content -Path $processJson -Encoding UTF8

Get-Content $processJson
