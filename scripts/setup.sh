#!/usr/bin/env bash
# ============================================================================
# Caixa Postal Hackathon — Setup do participante
#
# Rode este script DEPOIS de configurar o AWS CLI com suas credenciais.
# Ele deploya a stack completa na sua conta e gera um arquivo
# hackathon-info.txt com todas as URLs e credenciais que voce precisa.
#
# Uso:
#   Linux/macOS:
#     chmod +x participant-setup.sh && ./participant-setup.sh
#
#   Windows (PowerShell — veja participant-setup.ps1 pra versao nativa):
#     bash participant-setup.sh   # via WSL ou Git Bash
#
# Pre-requisitos:
#   - AWS CLI v2 configurado (aws sts get-caller-identity deve funcionar)
#   - Python 3.10+ (python3 --version)
#   - Docker (docker --version) — para build do container
#   - zip (zip --version)
#
# Tempo: ~15-20 minutos na primeira execucao. Idempotente — rode de novo
# se falhar no meio.
# ============================================================================
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'
ok()   { printf "${GREEN}✓${NC} %s\n" "$*"; }
info() { printf "${CYAN}→${NC} %s\n" "$*"; }
fail() { printf "${RED}✗${NC} %s\n" "$*" >&2; exit 1; }

# ── Pre-flight checks ────────────────────────────────────────────────
info "Verificando pre-requisitos..."

command -v aws   >/dev/null 2>&1 || fail "AWS CLI nao encontrado. Instale: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
command -v zip   >/dev/null 2>&1 || fail "zip nao encontrado. Instale: apt install zip / brew install zip / choco install zip"
command -v python3 >/dev/null 2>&1 || fail "Python 3 nao encontrado. Instale 3.10+: https://www.python.org/downloads/"

ACCOUNT=$(aws sts get-caller-identity --query Account --output text 2>/dev/null) || fail "AWS CLI nao autenticado. Rode: aws configure (ou exporte AWS_ACCESS_KEY_ID etc)"
REGION="${AWS_REGION:-us-east-1}"
export AWS_DEFAULT_REGION="$REGION"

info "Conta AWS: $ACCOUNT"
info "Regiao: $REGION"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

BUCKET="caixa-$ACCOUNT"
COG_DOMAIN="caixa-$ACCOUNT"

deploy() {
  local stack="$1" template="$2"; shift 2
  info "Deploy $stack..."
  aws cloudformation deploy \
    --stack-name "$stack" \
    --template-file "$template" \
    --capabilities CAPABILITY_NAMED_IAM CAPABILITY_IAM \
    --no-fail-on-empty-changeset \
    "$@"
  ok "$stack"
}

# ── 1. Shared infra (DDB + S3 + ECR + CodeBuild) ────────────────────
deploy caixa-shared infra/shared.yaml

# ── 2. Container image via CodeBuild (avoids ARM/x86 issues on Mac) ──
REPO="caixa"
ECR="$ACCOUNT.dkr.ecr.$REGION.amazonaws.com/$REPO"
info "Uploading source + building container via CodeBuild..."
zip -qr /tmp/caixa-src.zip . -x '.git/*' '__pycache__/*' '.env' '*.pyc' 'test-results/*'
aws s3 cp /tmp/caixa-src.zip "s3://$BUCKET/caixa-src.zip" >/dev/null
BUILD_ID=$(aws codebuild start-build --project-name caixa-build --query "build.id" --output text)
info "Build iniciado: $BUILD_ID (aguardando...)"
while true; do
  STATUS=$(aws codebuild batch-get-builds --ids "$BUILD_ID" --query "builds[0].buildStatus" --output text)
  [ "$STATUS" != "IN_PROGRESS" ] && break
  sleep 10
done
[ "$STATUS" != "SUCCEEDED" ] && fail "CodeBuild falhou: $STATUS. Verifique no console."
ok "Container pushed via CodeBuild: $ECR:latest"

# ── 4. Cognito ───────────────────────────────────────────────────────
deploy caixa-cognito infra/cognito.yaml \
  --parameter-overrides "HostedUIDomainPrefix=$COG_DOMAIN" \
    "CallbackUrls=http://localhost:8080/auth/callback,https://placeholder.cloudfront.net/auth/callback"

# ── 5. Lambda tools ──────────────────────────────────────────────────
TOOLS_ZIP="$HERE/.build-tools.zip"
(cd lambda_tools && zip -qr "$TOOLS_ZIP" app.py)
aws s3 cp "$TOOLS_ZIP" "s3://$BUCKET/lambda-tools.zip" >/dev/null
deploy caixa-tools-lambda infra/lambda-tools.yaml \
  --parameter-overrides "TableName=caixa-items" "CodeBucket=$BUCKET" "CodeKey=lambda-tools.zip"

# ── 6. Gateway ───────────────────────────────────────────────────────
USERPOOL_ID=$(aws cloudformation list-exports --query "Exports[?Name=='CaixaUserPoolId'].Value" --output text)
SVC_CLIENT=$(aws cloudformation list-exports --query "Exports[?Name=='CaixaServiceClientId'].Value" --output text)
DISCOVERY="https://cognito-idp.$REGION.amazonaws.com/$USERPOOL_ID/.well-known/openid-configuration"
GW_NAME="caixa-gw-${ACCOUNT:0:6}"
deploy caixa-gateway infra/gateway.yaml \
  --parameter-overrides "GatewayName=$GW_NAME" "UserPoolDiscoveryUrl=$DISCOVERY" "AllowedAudiences=$SVC_CLIENT"

# ── 7. Guardrail ─────────────────────────────────────────────────────
deploy caixa-guardrail infra/guardrails.yaml
GUARDRAIL_ID=$(aws cloudformation list-exports --query "Exports[?Name=='CaixaGuardrailId'].Value" --output text)
GUARDRAIL_VER=$(aws cloudformation list-exports --query "Exports[?Name=='CaixaGuardrailVersion'].Value" --output text)

# ── 8. VPC + subnets ─────────────────────────────────────────────────
VPC=$(aws ec2 describe-vpcs --filters "Name=is-default,Values=true" --query "Vpcs[0].VpcId" --output text)
SUBNETS=$(aws ec2 describe-subnets --filters "Name=vpc-id,Values=$VPC" --query "Subnets[:2].SubnetId" --output text | tr '\t' ',')
[ "$VPC" = "None" ] && fail "Nenhuma VPC default encontrada. Crie uma no console: VPC → Your VPCs → Create default VPC"

# ── 9. ECS ───────────────────────────────────────────────────────────
# OIDC issuer for the central Cognito (Elevata's pool — shared across all
# participant stacks so everyone logs in with the same account).
ISSUER="https://cognito-idp.$REGION.amazonaws.com/$USERPOOL_ID"
WEB_CLIENT=$(aws cloudformation list-exports --query "Exports[?Name=='CaixaWebClientId'].Value" --output text)

deploy caixa-ecs infra/ecs.yaml \
  --parameter-overrides "ImageUri=$ECR:latest" "BucketName=$BUCKET" "TableName=caixa-items" \
    "VpcId=$VPC" "SubnetIds=$SUBNETS" \
    "GuardrailId=$GUARDRAIL_ID" "GuardrailVersion=$GUARDRAIL_VER" \
    "AuthEnabled=false"
ALB_DNS=$(aws cloudformation list-exports --query "Exports[?Name=='CaixaAlbDns'].Value" --output text)

# ── 10. Gateway target (boto3) ───────────────────────────────────────
info "Registrando Gateway target (Smithy → Lambda)..."
python3 infra/create_gateway_target.py 2>&1 | tail -2
ok "Gateway target registrado"

# ── Output ───────────────────────────────────────────────────────────
WEB_CLIENT=$(aws cloudformation list-exports --query "Exports[?Name=='CaixaWebClientId'].Value" --output text)

cat > hackathon-info.txt << INFO
============================================================
 CAIXA POSTAL HACKATHON — Informacoes da sua stack
 Gerado em: $(date)
 Conta AWS: $ACCOUNT
============================================================

App URL:             http://$ALB_DNS

Cognito:
  User Pool ID:      $USERPOOL_ID
  Web Client ID:     $WEB_CLIENT
  Service Client ID: $SVC_CLIENT
  Domain:            https://$COG_DOMAIN.auth.$REGION.amazoncognito.com

Guardrail:           $GUARDRAIL_ID v$GUARDRAIL_VER

DynamoDB Table:      caixa-items
S3 Bucket:           $BUCKET
ECR:                 $ECR

Para redeploy do container apos editar codigo:
  zip -qr /tmp/caixa-src.zip . -x '.git/*' '__pycache__/*' '.env'
  aws s3 cp /tmp/caixa-src.zip s3://$BUCKET/caixa-src.zip
  aws codebuild start-build --project-name caixa-build
  aws ecs update-service --cluster caixa --service caixa --force-new-deployment

Desafio + chat:      https://d168tci1ssss8v.cloudfront.net/desafio

NOTA: Audio (microfone) so funciona em HTTPS. Se precisar,
adicione CloudFront depois: scripts/add-cloudfront.sh
============================================================
INFO

echo
echo "════════════════════════════════════════════════════════"
printf " ${GREEN}Setup concluido!${NC}\n"
echo "════════════════════════════════════════════════════════"
echo
cat hackathon-info.txt
echo
info "Arquivo hackathon-info.txt salvo na raiz do projeto."
info "Abra no browser: http://$ALB_DNS"
