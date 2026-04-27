#!/usr/bin/env bash
# ============================================================================
# Caixa Postal — Redeploy rapido
#
# Usa quando voce editou codigo (agents_config.yaml, tools.py, etc)
# e quer subir as mudancas pro ECS sem recriar toda a infra.
#
# Uso:
#   bash scripts/deploy.sh
#
# Tempo: ~5 min (CodeBuild ~3 min + ECS rolling ~2 min)
# ============================================================================
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'
ok()   { printf "${GREEN}✓${NC} %s\n" "$*"; }
info() { printf "${CYAN}→${NC} %s\n" "$*"; }
fail() { printf "${RED}✗${NC} %s\n" "$*" >&2; exit 1; }

ACCOUNT=$(aws sts get-caller-identity --query Account --output text 2>/dev/null) || fail "AWS CLI nao autenticado"
REGION="${AWS_REGION:-us-east-1}"
export AWS_DEFAULT_REGION="$REGION"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

BUCKET="caixa-$ACCOUNT"

info "Conta: $ACCOUNT | Regiao: $REGION"

# 1. Zip + upload
info "Empacotando codigo..."
zip -qr /tmp/caixa-src.zip . -x '.git/*' '__pycache__/*' '.env' '*.pyc' 'test-results/*'
aws s3 cp /tmp/caixa-src.zip "s3://$BUCKET/caixa-src.zip" >/dev/null
ok "Upload S3"

# 2. CodeBuild
BUILD_ID=$(aws codebuild start-build --project-name caixa-build --query "build.id" --output text)
info "Build: $BUILD_ID (aguardando...)"
while true; do
  STATUS=$(aws codebuild batch-get-builds --ids "$BUILD_ID" --query "builds[0].buildStatus" --output text)
  [ "$STATUS" != "IN_PROGRESS" ] && break
  sleep 10
done
[ "$STATUS" != "SUCCEEDED" ] && fail "CodeBuild falhou: $STATUS"
ok "Container atualizado"

# 3. ECS force deploy
aws ecs update-service --cluster caixa --service caixa --force-new-deployment >/dev/null
ok "ECS redeploy disparado"

ALB_DNS=$(aws cloudformation list-exports --query "Exports[?Name=='CaixaAlbDns'].Value" --output text 2>/dev/null || echo "")

echo
echo "════════════════════════════════════════════════════════"
printf " ${GREEN}Deploy concluido!${NC}\n"
echo "════════════════════════════════════════════════════════"
[ -n "$ALB_DNS" ] && echo "  App: http://$ALB_DNS"
echo "  ECS leva ~2 min pra trocar os containers."
echo
