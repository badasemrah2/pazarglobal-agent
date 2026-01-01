param(
  [int]$Port = 8016
)

$ErrorActionPreference = 'Stop'

$projectDir = "c:\Users\emrah badas\OneDrive\Desktop\deneme\pazarglobal-agent"
Set-Location $projectDir

# Start server
$proc = Start-Process -FilePath python -ArgumentList @(
  '-m','uvicorn','main:app',
  '--host','127.0.0.1',
  '--port', $Port
) -PassThru -WindowStyle Hidden

try {
  # Wait for health
  $ready = $false
  for ($i=0; $i -lt 20; $i++) {
    try {
      $null = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 2
      $ready = $true
      break
    } catch {
      Start-Sleep -Seconds 1
    }
  }

  if (-not $ready) {
    throw "Server did not become ready on port $Port"
  }

  # Pick any existing profile id (needed due to FK constraints)
  $userId = python -c "from services.supabase_client import SupabaseClient; c=SupabaseClient(); r=c.client.table('profiles').select('id').limit(1).execute(); print(r.data[0]['id'])"

  $body = @{ message = 'ilan oluştur'; session_id = 'http-smoke-session'; user_id = $userId } | ConvertTo-Json
  $resp = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:$Port/webchat/message" -ContentType 'application/json' -Body $body -TimeoutSec 30

  Write-Output "HEALTH_OK=true"
  Write-Output ("CHAT_SUCCESS=" + $resp.success)
  Write-Output ("CHAT_INTENT=" + $resp.intent)
  Write-Output ("CHAT_TYPE=" + $resp.data.type)
  Write-Output ("CHAT_MESSAGE_PREFIX=" + (($resp.message ?? '') -replace "\r?\n"," ").Substring(0,[Math]::Min(120,(($resp.message ?? '')).Length)))
}
finally {
  if ($proc -and -not $proc.HasExited) {
    Stop-Process -Id $proc.Id -Force
  }
}
