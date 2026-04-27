# ============================================================================
# Caixa Postal - Teardown completo
#
# Remove todos os recursos AWS criados pelo setup.ps1.
# Uso: .\scripts\teardown.ps1
# ============================================================================
$ErrorActionPreference = 'Stop'

function ok   { param([string]$msg) Write-Host "[OK] $msg" -ForegroundColor Green }
function info { param([string]$msg) Write-Host "[->] $msg" -ForegroundColor Cyan }
function fail { param([string]$msg) Write-Host "[X] $msg" -ForegroundColor Red; exit 1 }

try {
    $Account = aws sts get-caller-identity --query Account --output text 2>$null
    if (-not $Account) { throw "vazio" }
} catch {
    fail "AWS CLI nao autenticado"
}

$Region = if ($env:AWS_REGION) { $env:AWS_REGION } else { "us-east-1" }
$env:AWS_DEFAULT_REGION = $Region

Write-Host ""
Write-Host "================================================================" -ForegroundColor Red
Write-Host " ATENCAO: vai DELETAR todos os recursos caixa-* na conta $Account" -ForegroundColor Red
Write-Host "================================================================" -ForegroundColor Red
Write-Host ""
$Confirm = Read-Host "Tem certeza? (digite 'sim' pra confirmar)"
if ($Confirm -ne "sim") { Write-Host "Cancelado."; exit 0 }

info "Deletando stacks CFN..."

# Delete in reverse dependency order
foreach ($s in @("caixa-ecs", "caixa-guardrail")) {
    try { aws cloudformation delete-stack --stack-name $s 2>$null; info "  $s" } catch {}
}
foreach ($s in @("caixa-ecs", "caixa-guardrail")) {
    try { aws cloudformation wait stack-delete-complete --stack-name $s 2>$null } catch {}
}
ok "ECS + guardrail removidos"

# Gateway: delete targets + resource first
info "Limpando gateways..."
try {
    $Gateways = aws bedrock-agentcore-control list-gateways --query "items[*].gatewayId" --output text 2>$null
    if ($Gateways -and $Gateways -ne "None") {
        foreach ($gw in ($Gateways -split "\s+")) {
            if (-not $gw) { continue }
            try {
                $Targets = aws bedrock-agentcore-control list-gateway-targets --gateway-identifier $gw --query "items[*].targetId" --output text 2>$null
                if ($Targets -and $Targets -ne "None") {
                    foreach ($t in ($Targets -split "\s+")) {
                        if (-not $t) { continue }
                        aws bedrock-agentcore-control delete-gateway-target --gateway-identifier $gw --target-id $t 2>$null
                    }
                }
            } catch {}
            Start-Sleep -Seconds 5
            try { aws bedrock-agentcore-control delete-gateway --gateway-identifier $gw 2>$null } catch {}
        }
    }
} catch {}
try { aws cloudformation delete-stack --stack-name caixa-gateway 2>$null } catch {}
try { aws cloudformation wait stack-delete-complete --stack-name caixa-gateway 2>$null } catch {}
ok "Gateway removido"

# Lambda + Cognito
foreach ($s in @("caixa-tools-lambda", "caixa-cognito")) {
    try { aws cloudformation delete-stack --stack-name $s 2>$null } catch {}
}
foreach ($s in @("caixa-tools-lambda", "caixa-cognito")) {
    try { aws cloudformation wait stack-delete-complete --stack-name $s 2>$null } catch {}
}
ok "Lambda + Cognito removidos"

# Shared (S3 + ECR + DynamoDB) - need to empty first
info "Esvaziando S3 e ECR..."
try { aws s3 rm "s3://caixa-$Account" --recursive 2>$null } catch {}
try { aws ecr delete-repository --repository-name caixa --force 2>$null } catch {}
try { aws cloudformation delete-stack --stack-name caixa-shared 2>$null } catch {}
try { aws cloudformation wait stack-delete-complete --stack-name caixa-shared 2>$null } catch {}
ok "Shared removido"

Write-Host ""
Write-Host "================================================================" -ForegroundColor Green
Write-Host " Teardown concluido! Conta $Account limpa." -ForegroundColor Green
Write-Host "================================================================" -ForegroundColor Green
