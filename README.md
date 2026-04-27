# Caixa Postal Universal — AWS AI Hackathon Starter Kit

WhatsApp-clone com agentes de IA. Bedrock + Strands Agents + AG-UI. Deploy automatizado na sua conta AWS.

## Setup rapido

```bash
# 1. Configure suas credenciais AWS (SSO do evento)
aws sts get-caller-identity  # confirme a conta

# 2. Ative os modelos no Bedrock (Console → Bedrock → Model access)
#    Marque: Claude Sonnet 4.5 + Claude Haiku 4.5

# 3. Clone e rode o setup
git clone https://github.com/elvt-amati/caixa-postal-hackathon.git caixa && cd caixa
bash scripts/setup.sh       # ~10 min, cria toda a infra

# 4. Abra a URL do ALB que apareceu no terminal
```

## Scripts

| Script | O que faz | Quando usar |
|---|---|---|
| `scripts/setup.sh` | Cria toda a infra do zero (DDB, Cognito, Lambda, Gateway, Guardrail, ECS, CodeBuild) | Primeira vez |
| `scripts/deploy.sh` | Rebuilda container + atualiza ECS (~5 min) | Apos editar codigo |
| `scripts/teardown.sh` | Remove todos os recursos AWS da conta | Limpeza final |

## Adicionar um agente (5 min)

1. Edite `backend/agents_config.yaml`:

```yaml
meu_agente:
  display_name: "Meu Agente"
  emoji: "⚡"
  description: "Faz X a partir de foto ou audio"
  system_prompt: |
    Voce e um assistente que faz X.
    Responda em portugues.
  tools:
    - create_task
    - render_chart
    - publish_card
```

2. Teste local: `cd backend && uvicorn main:app --reload --port 8080`
3. Publique: `bash scripts/deploy.sh`

## Tools disponiveis

```
create_task · create_payment · create_reminder · save_contact · save_note · send_email
list_tasks · list_payments · list_reminders · update_item · delete_item · undo_delete
render_chart · publish_card · search_web · generate_pitch_deck
```

Todas user-scoped por `guest_id` — cada browser tem seu proprio silo de dados.

## Plugar MCP server

```yaml
meu_agente:
  mcp_servers:
    - command: "npx"
      args: ["-y", "@modelcontextprotocol/server-github"]
      env: {GITHUB_PERSONAL_ACCESS_TOKEN: "${GH_TOKEN}"}
```

Qualquer MCP server stdio ou SSE funciona. Reinicie o uvicorn — tools aparecem auto.

## BeautyCore API (trilha avancada)

API REST ficticia protegida por JWT. O conector ja esta na sidebar do app.

**Obter JWT programaticamente:**
```bash
# Via sua propria instancia (mais facil):
curl -s -X POST http://<seu-ALB>/api/connections/beautycore/login \
  -H "content-type: application/json" \
  -d '{"email":"admin@beautycore.com.br","password":"BeautyAdmin2026!"}'
# Retorna: {ok, full_id_token, api_url, groups}
```

**Usuarios:**

| Email | Senha | Grupo |
|---|---|---|
| admin@beautycore.com.br | BeautyAdmin2026! | admin (CRUD total) |
| analista@beautycore.com.br | BeautyRead2026! | analista (leitura) |
| estoque@beautycore.com.br | BeautyStock2026! | estoque (inventario) |

**Endpoints:**

| Metodo | Rota | Descricao | Quem pode |
|---|---|---|---|
| GET | /produtos | Lista produtos (query: ?categoria=batom) | todos |
| GET | /produtos/{sku} | Detalhe de 1 produto | todos |
| GET | /clientes | Lista clientes | admin, analista |
| GET | /clientes/{id} | Detalhe de 1 cliente | admin, analista |
| GET | /pedidos | Lista pedidos | admin, analista |
| GET | /pedidos/{id} | Detalhe de 1 pedido | admin, analista |
| POST | /pedidos | Criar pedido | admin |
| PATCH | /produtos/{sku}/estoque | Atualizar estoque | admin, estoque |

**Exemplo:**
```bash
TOKEN="<full_id_token do login>"
curl -s "https://i3nc4271ve.execute-api.us-east-1.amazonaws.com/produtos?categoria=batom" \
  -H "Authorization: Bearer $TOKEN"
```

## Arquitetura

```
Browser → ALB → ECS Fargate (Strands Agent + Bedrock Claude) → DynamoDB
                     ↕ AG-UI SSE          ↕ MCP tools
              AgentCore Gateway → Lambda → DynamoDB/S3/SES/Transcribe
```

**Stack:** Bedrock (Claude Sonnet 4.5) · Strands Agents · AG-UI · AgentCore Gateway (Smithy) · ECS Fargate · DynamoDB · Cognito · CodeBuild · CloudFormation

## Links do evento

| O que | URL |
|---|---|
| Desafio | https://d168tci1ssss8v.cloudfront.net/desafio |
| Chat do evento | https://d168tci1ssss8v.cloudfront.net/chat |
| Demo Elevata | https://d168tci1ssss8v.cloudfront.net |
| Cheatsheet | https://d168tci1ssss8v.cloudfront.net/cheatsheet |
