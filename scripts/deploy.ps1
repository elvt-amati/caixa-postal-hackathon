# ============================================================================
# Caixa Postal - Redeploy rapido
#
# Usa quando voce editou codigo (agents_config.yaml, tools.py, etc)
# e quer subir as mudancas pro ECS sem recriar toda a infra.
#
# Uso:
#   .\scripts\deploy.ps1
#
# Tempo: ~5 min (CodeBuild ~3 min + ECS rolling ~2 min)
# ============================================================================
$ErrorActionPreference = 'Stop'

function ok   { param([string]$msg) Write-Host "[OK] $msg" -ForegroundColor Green }
function info { param([string]$msg) Write-Host "[->] $msg" -ForegroundColor Cyan }
function fail { param([string]$msg) Write-Host "[X] $msg" -ForegroundColor Red; exit 1 }

# Helper: cria zip excluindo patterns
function New-SourceZip {
    param(
        [string]$SourceDir,
        [string]$DestZip,
        [string[]]$ExcludePatterns = @('.git', '__pycache__', '.env', '*.pyc', 'test-results')
    )
    $TempStaging = Join-Path ([System.IO.Path]::GetTempPath()) "caixa-zip-staging-$(Get-Random)"
    if (Test-Path $TempStaging) { Remove-Item $TempStaging -Recurse -Force }
    New-Item -ItemType Directory -Path $TempStaging | Out-Null

    $Items = Get-ChildItem -Path $SourceDir -Recurse -Force | Where-Object {
        $rel = $_.FullName.Substring($SourceDir.Length + 1)
        foreach ($pat in $ExcludePatterns) {
            if ($rel -like "$pat*" -or $rel -like "*\$pat*" -or $rel -like "*/$pat*" -or $_.Name -like $pat) {
                return $false
            }
        }
        return $true
    }

    foreach ($item in $Items) {
        $rel = $item.FullName.Substring($SourceDir.Length + 1)
        $dest = Join-Path $TempStaging $rel
        if ($item.PSIsContainer) {
            New-Item -ItemType Directory -Path $dest -Force | Out-Null
        } else {
            $destDir = Split-Path $dest -Parent
            if (-not (Test-Path $destDir)) { New-Item -ItemType Directory -Path $destDir -Force | Out-Null }
            Copy-Item $item.FullName $dest
        }
    }

    if (Test-Path $DestZip) { Remove-Item $DestZip -Force }
    Compress-Archive -Path "$TempStaging\*" -DestinationPath $DestZip -Force
    Remove-Item $TempStaging -Recurse -Force
}

try {
    $Account = aws sts get-caller-identity --query Account --output text 2>$null
    if (-not $Account) { throw "vazio" }
} catch {
    fail "AWS CLI nao autenticado"
}

$Region = if ($env:AWS_REGION) { $env:AWS_REGION } else { "us-east-1" }
$env:AWS_DEFAULT_REGION = $Region

$HERE = (Resolve-Path "$PSScriptRoot/..").Path
Set-Location $HERE

$Bucket = "caixa-$Account"

info "Conta: $Account | Regiao: $Region"

# 1. Zip + upload
info "Empacotando codigo..."
$ZipPath = Join-Path ([System.IO.Path]::GetTempPath()) "caixa-src.zip"
New-SourceZip -SourceDir $HERE -DestZip $ZipPath
aws s3 cp $ZipPath "s3://$Bucket/caixa-src.zip" | Out-Null
ok "Upload S3"

# 2. CodeBuild
$BuildId = aws codebuild start-build --project-name caixa-build --query "build.id" --output text
info "Build: $BuildId (aguardando...)"
while ($true) {
    $Status = aws codebuild batch-get-builds --ids $BuildId --query "builds[0].buildStatus" --output text
    if ($Status -ne "IN_PROGRESS") { break }
    Start-Sleep -Seconds 10
}
if ($Status -ne "SUCCEEDED") { fail "CodeBuild falhou: $Status" }
ok "Container atualizado"

# 3. ECS force deploy
aws ecs update-service --cluster caixa --service caixa --force-new-deployment | Out-Null
ok "ECS redeploy disparado"

try {
    $AlbDns = aws cloudformation list-exports --query "Exports[?Name=='CaixaAlbDns'].Value" --output text 2>$null
} catch {
    $AlbDns = ""
}

Write-Host ""
Write-Host "================================================================" -ForegroundColor Green
Write-Host " Deploy concluido!" -ForegroundColor Green
Write-Host "================================================================" -ForegroundColor Green
if ($AlbDns) { Write-Host "  App: http://$AlbDns" }
Write-Host "  ECS leva ~2 min pra trocar os containers."
Write-Host ""
