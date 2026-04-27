#!/usr/bin/env bash
# ============================================================================
# Caixa Postal — Teardown completo
#
# Remove todos os recursos AWS criados pelo setup.sh.
# Uso: bash scripts/teardown.sh
# ============================================================================
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'
ok()   { printf "${GREEN}✓${NC} %s\n" "$*"; }
info() { printf "${CYAN}→${NC} %s\n" "$*"; }
fail() { printf "${RED}✗${NC} %s\n" "$*" >&2; exit 1; }

ACCOUNT=$(aws sts get-caller-identity --query Account --output text 2>/dev/null) || fail "AWS CLI nao autenticado"
REGION="${AWS_REGION:-us-east-1}"
export AWS_DEFAULT_REGION="$REGION"

echo
printf "${RED}════════════════════════════════════════════════════════${NC}\n"
printf " ${RED}ATENCAO: vai DELETAR todos os recursos caixa-* na conta $ACCOUNT${NC}\n"
printf "${RED}════════════════════════════════════════════════════════${NC}\n"
echo
read -p "Tem certeza? (digite 'sim' pra confirmar): " CONFIRM
[ "$CONFIRM" != "sim" ] && echo "Cancelado." && exit 0

info "Deletando stacks CFN..."

# Delete in reverse dependency order
for s in caixa-ecs caixa-guardrail; do
  aws cloudformation delete-stack --stack-name $s 2>/dev/null && info "  $s"
done
for s in caixa-ecs caixa-guardrail; do
  aws cloudformation wait stack-delete-complete --stack-name $s 2>/dev/null
done
ok "ECS + guardrail removidos"

# Gateway: delete targets + resource first
info "Limpando gateways..."
for gw in $(aws bedrock-agentcore-control list-gateways --query "items[*].gatewayId" --output text 2>/dev/null); do
  [ -z "$gw" ] && continue
  for t in $(aws bedrock-agentcore-control list-gateway-targets --gateway-identifier "$gw" --query "items[*].targetId" --output text 2>/dev/null); do
    aws bedrock-agentcore-control delete-gateway-target --gateway-identifier "$gw" --target-id "$t" 2>/dev/null
  done
  sleep 5
  aws bedrock-agentcore-control delete-gateway --gateway-identifier "$gw" 2>/dev/null
done
aws cloudformation delete-stack --stack-name caixa-gateway 2>/dev/null
aws cloudformation wait stack-delete-complete --stack-name caixa-gateway 2>/dev/null
ok "Gateway removido"

# Lambda + Cognito
for s in caixa-tools-lambda caixa-cognito; do
  aws cloudformation delete-stack --stack-name $s 2>/dev/null
done
for s in caixa-tools-lambda caixa-cognito; do
  aws cloudformation wait stack-delete-complete --stack-name $s 2>/dev/null
done
ok "Lambda + Cognito removidos"

# Shared (S3 + ECR + DynamoDB) — need to empty first
info "Esvaziando S3 e ECR..."
aws s3 rm "s3://caixa-$ACCOUNT" --recursive 2>/dev/null
aws ecr delete-repository --repository-name caixa --force 2>/dev/null
aws cloudformation delete-stack --stack-name caixa-shared 2>/dev/null
aws cloudformation wait stack-delete-complete --stack-name caixa-shared 2>/dev/null
ok "Shared removido"

echo
printf "${GREEN}════════════════════════════════════════════════════════${NC}\n"
printf " ${GREEN}Teardown concluido! Conta $ACCOUNT limpa.${NC}\n"
printf "${GREEN}════════════════════════════════════════════════════════${NC}\n"
