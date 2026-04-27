# ============================================================================
# Caixa Postal Hackathon - Setup do participante
#
# Rode este script DEPOIS de configurar o AWS CLI com suas credenciais.
# Ele deploya a stack completa na sua conta e gera um arquivo
# hackathon-info.txt com todas as URLs e credenciais que voce precisa.
#
# Uso:
#   PowerShell:
#     .\scripts\setup.ps1
#
# Pre-requisitos:
#   - AWS CLI v2 configurado (aws sts get-caller-identity deve funcionar)
#   - Python 3.10+ (python --version)
#   - Docker (docker --version) - para build do container
#
# Tempo: ~15-20 minutos na primeira execucao. Idempotente - rode de novo
# se falhar no meio.
# ============================================================================
$ErrorActionPreference = 'Stop'

function ok   { param([string]$msg) Write-Host "[OK] $msg" -ForegroundColor Green }
function info { param([string]$msg) Write-Host "[->] $msg" -ForegroundColor Cyan }
function fail { param([string]$msg) Write-Host "[X] $msg" -ForegroundColor Red; exit 1 }

# Helper: cria zip excluindo patterns (Compress-Archive nao suporta exclusao)
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

# ── Pre-flight checks ────────────────────────────────────────────────
info "Verificando pre-requisitos..."

if (-not (Get-Command aws -ErrorAction SilentlyContinue)) { fail "AWS CLI nao encontrado. Instale: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html" }
if (-not (Get-Command python -ErrorAction SilentlyContinue)) { fail "Python nao encontrado. Instale 3.10+: https://www.python.org/downloads/" }

try {
    $Account = aws sts get-caller-identity --query Account --output text 2>$null
    if (-not $Account) { throw "vazio" }
} catch {
    fail "AWS CLI nao autenticado. Rode: aws configure (ou exporte AWS_ACCESS_KEY_ID etc)"
}

$Region = if ($env:AWS_REGION) { $env:AWS_REGION } else { "us-east-1" }
$env:AWS_DEFAULT_REGION = $Region

info "Conta AWS: $Account"
info "Regiao: $Region"

$HERE = (Resolve-Path "$PSScriptRoot/..").Path
Set-Location $HERE

$Bucket = "caixa-$Account"
$CogDomain = "caixa-$Account"

function deploy {
    param(
        [string]$Stack,
        [string]$Template,
        [Parameter(ValueFromRemainingArguments)]
        [string[]]$ExtraArgs
    )
    info "Deploy $Stack..."
    $cmd = @(
        "cloudformation", "deploy",
        "--stack-name", $Stack,
        "--template-file", $Template,
        "--capabilities", "CAPABILITY_NAMED_IAM", "CAPABILITY_IAM",
        "--no-fail-on-empty-changeset"
    )
    if ($ExtraArgs) { $cmd += $ExtraArgs }
    & aws @cmd
    if ($LASTEXITCODE -ne 0) { fail "Falha no deploy de $Stack" }
    ok $Stack
}

# ── 1. Shared infra (DDB + S3 + ECR + CodeBuild) ────────────────────
deploy "caixa-shared" "infra/shared.yaml"

# ── 2. Container image via CodeBuild (avoids ARM/x86 issues on Mac) ──
$Repo = "caixa"
$ECR = "$Account.dkr.ecr.$Region.amazonaws.com/$Repo"
info "Uploading source + building container via CodeBuild..."

$ZipPath = Join-Path ([System.IO.Path]::GetTempPath()) "caixa-src.zip"
New-SourceZip -SourceDir $HERE -DestZip $ZipPath
aws s3 cp $ZipPath "s3://$Bucket/caixa-src.zip" | Out-Null

$BuildId = aws codebuild start-build --project-name caixa-build --query "build.id" --output text
info "Build iniciado: $BuildId (aguardando...)"
while ($true) {
    $Status = aws codebuild batch-get-builds --ids $BuildId --query "builds[0].buildStatus" --output text
    if ($Status -ne "IN_PROGRESS") { break }
    Start-Sleep -Seconds 10
}
if ($Status -ne "SUCCEEDED") { fail "CodeBuild falhou: $Status. Verifique no console." }
ok "Container pushed via CodeBuild: ${ECR}:latest"

# ── 4. Cognito ───────────────────────────────────────────────────────
deploy "caixa-cognito" "infra/cognito.yaml" `
    "--parameter-overrides" "HostedUIDomainPrefix=$CogDomain" `
    "CallbackUrls=http://localhost:8080/auth/callback,https://placeholder.cloudfront.net/auth/callback"

# ── 5. Lambda tools ──────────────────────────────────────────────────
$ToolsZip = Join-Path $HERE ".build-tools.zip"
Push-Location (Join-Path $HERE "lambda_tools")
if (Test-Path $ToolsZip) { Remove-Item $ToolsZip -Force }
Compress-Archive -Path "app.py" -DestinationPath $ToolsZip -Force
Pop-Location
aws s3 cp $ToolsZip "s3://$Bucket/lambda-tools.zip" | Out-Null
deploy "caixa-tools-lambda" "infra/lambda-tools.yaml" `
    "--parameter-overrides" "TableName=caixa-items" "CodeBucket=$Bucket" "CodeKey=lambda-tools.zip"

# ── 6. Gateway ───────────────────────────────────────────────────────
$UserpoolId = aws cloudformation list-exports --query "Exports[?Name=='CaixaUserPoolId'].Value" --output text
$SvcClient = aws cloudformation list-exports --query "Exports[?Name=='CaixaServiceClientId'].Value" --output text
$Discovery = "https://cognito-idp.$Region.amazonaws.com/$UserpoolId/.well-known/openid-configuration"
$GwName = "caixa-gw-$($Account.Substring(0,6))"
deploy "caixa-gateway" "infra/gateway.yaml" `
    "--parameter-overrides" "GatewayName=$GwName" "UserPoolDiscoveryUrl=$Discovery" "AllowedAudiences=$SvcClient"

# ── 7. Guardrail ─────────────────────────────────────────────────────
deploy "caixa-guardrail" "infra/guardrails.yaml"
$GuardrailId = aws cloudformation list-exports --query "Exports[?Name=='CaixaGuardrailId'].Value" --output text
$GuardrailVer = aws cloudformation list-exports --query "Exports[?Name=='CaixaGuardrailVersion'].Value" --output text

# ── 8. VPC + subnets ─────────────────────────────────────────────────
$VPC = aws ec2 describe-vpcs --filters "Name=is-default,Values=true" --query "Vpcs[0].VpcId" --output text
$SubnetsRaw = aws ec2 describe-subnets --filters "Name=vpc-id,Values=$VPC" --query "Subnets[:2].SubnetId" --output text
$Subnets = $SubnetsRaw.Replace("`t", ",")
if ($VPC -eq "None") { fail "Nenhuma VPC default encontrada. Crie uma no console: VPC -> Your VPCs -> Create default VPC" }

# ── 9. ECS ───────────────────────────────────────────────────────────
# OIDC issuer for the central Cognito (Elevata's pool - shared across all
# participant stacks so everyone logs in with the same account).
$Issuer = "https://cognito-idp.$Region.amazonaws.com/$UserpoolId"
$WebClient = aws cloudformation list-exports --query "Exports[?Name=='CaixaWebClientId'].Value" --output text

deploy "caixa-ecs" "infra/ecs.yaml" `
    "--parameter-overrides" "ImageUri=${ECR}:latest" "BucketName=$Bucket" "TableName=caixa-items" `
    "VpcId=$VPC" "SubnetIds=$Subnets" `
    "GuardrailId=$GuardrailId" "GuardrailVersion=$GuardrailVer" `
    "AuthEnabled=false"
$AlbDns = aws cloudformation list-exports --query "Exports[?Name=='CaixaAlbDns'].Value" --output text

# ── 10. Gateway target (boto3) ───────────────────────────────────────
info "Registrando Gateway target (Smithy -> Lambda)..."
python infra/create_gateway_target.py 2>&1 | Select-Object -Last 2
ok "Gateway target registrado"

# ── Output ───────────────────────────────────────────────────────────
$WebClient = aws cloudformation list-exports --query "Exports[?Name=='CaixaWebClientId'].Value" --output text

$InfoContent = @"
============================================================
 CAIXA POSTAL HACKATHON - Informacoes da sua stack
 Gerado em: $(Get-Date)
 Conta AWS: $Account
============================================================

App URL:             http://$AlbDns

Cognito:
  User Pool ID:      $UserpoolId
  Web Client ID:     $WebClient
  Service Client ID: $SvcClient
  Domain:            https://$CogDomain.auth.$Region.amazoncognito.com

Guardrail:           $GuardrailId v$GuardrailVer

DynamoDB Table:      caixa-items
S3 Bucket:           $Bucket
ECR:                 $ECR

Para redeploy do container apos editar codigo:
  .\scripts\deploy.ps1

Desafio + chat:      https://d168tci1ssss8v.cloudfront.net/desafio

NOTA: Audio (microfone) so funciona em HTTPS. Se precisar,
adicione CloudFront depois: scripts/add-cloudfront.sh
============================================================
"@

$InfoContent | Set-Content "hackathon-info.txt"

Write-Host ""
Write-Host "================================================================" -ForegroundColor Green
Write-Host " Setup concluido!" -ForegroundColor Green
Write-Host "================================================================" -ForegroundColor Green
Write-Host ""
Get-Content "hackathon-info.txt"
Write-Host ""
info "Arquivo hackathon-info.txt salvo na raiz do projeto."
info "Abra no browser: http://$AlbDns"
