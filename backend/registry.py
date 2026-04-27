"""Agent registry — loads agents.yaml, builds Strands agents, supports delegation."""
import os
from datetime import date
from pathlib import Path

import yaml
from strands import Agent, tool
from strands.models import CacheConfig
from strands.models.bedrock import BedrockModel

from tools import TOOLS_BY_NAME
from mcp_loader import load_mcp_tools

REGION = os.environ.get("AWS_REGION", "us-east-1")
MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")
# Optional — when set, all agent invocations are wrapped with Bedrock Guardrails
# (PII masking + prompt-attack filter). See infra/guardrails.yaml.
GUARDRAIL_ID = os.environ.get("BEDROCK_GUARDRAIL_ID", "")
GUARDRAIL_VERSION = os.environ.get("BEDROCK_GUARDRAIL_VERSION", "DRAFT")

CONFIG_PATH = Path(__file__).parent / "agents_config.yaml"


def _make_model() -> BedrockModel:
    """Build BedrockModel with optional guardrails + system-prompt caching.

    Prompt caching reduces cost by ~90% on repeated system prompts (each
    specialist has a long static system_prompt that's re-sent every turn).
    Guardrails run server-side at invoke time with minimal latency overhead.
    """
    extra: dict = {"temperature": 0.3}
    # Prompt caching has two layers:
    #   - cache_tools="default": inserts a cachePoint at the end of the tools
    #     block. Tools are identical across all threads for a given specialist
    #     (15 tool schemas ≈ ~2k tokens), so this hits the cache ON EVERY
    #     request from every user once the first request has populated it.
    #   - cache_config=CacheConfig(strategy="auto"): inserts a cachePoint at
    #     the end of the last user message. This caches the in-thread
    #     conversation prefix — each follow-up turn in the same thread reads
    #     from cache. Together they cover both cross-thread reuse (tools) and
    #     within-thread reuse (message history).
    # Claude cache TTL is 5 minutes; at event density (~70 users) the
    # tools block stays resident almost continuously.
    extra["cache_tools"] = "default"
    extra["cache_config"] = CacheConfig(strategy="auto")
    if GUARDRAIL_ID:
        # Accept both full ARN and bare ID; Strands wants just the ID portion
        gid = GUARDRAIL_ID.split("/")[-1] if "/" in GUARDRAIL_ID else GUARDRAIL_ID
        extra["guardrail_id"] = gid
        extra["guardrail_version"] = GUARDRAIL_VERSION
        # Default is redact_input=True, redact_output=False. Flip output on
        # so a triggered content filter scrubs the model's reply too, not just
        # the user turn.
        extra["guardrail_redact_output"] = True
    return BedrockModel(model_id=MODEL_ID, region_name=REGION, **extra)


def _with_date(sp: str) -> str:
    """Prepend current date to system prompt so the model knows 'today'."""
    return f"Data de hoje: {date.today().strftime('%d/%m/%Y')}.\n\n{sp}"


class AgentRegistry:
    """Holds all specialist Strands agents + the Concierge orchestrator."""

    def __init__(self):
        with CONFIG_PATH.open("r", encoding="utf-8") as f:
            self.config = yaml.safe_load(f)
        self.specialists: dict[str, Agent] = {}
        self.specialist_meta: dict[str, dict] = {}
        # Per-specialist tool list + system prompt — used to spin fresh
        # Agent instances per synchronous call() so concurrent users don't
        # share the template agent's ``messages`` history.
        self._templates: dict[str, dict] = {}
        self._build_specialists()

    def _build_specialists(self):
        for name, cfg in self.config.items():
            if name == "concierge":
                continue
            tools = [TOOLS_BY_NAME[t] for t in cfg.get("tools", []) if t in TOOLS_BY_NAME]
            tools.extend(load_mcp_tools(cfg.get("mcp_servers")))
            self._templates[name] = {"tools": tools, "system_prompt": cfg["system_prompt"]}
            self.specialists[name] = Agent(
                model=_make_model(),
                tools=tools,
                system_prompt=_with_date(cfg["system_prompt"]),
            )
            self.specialist_meta[name] = {
                "display_name": cfg.get("display_name", name),
                "emoji": cfg.get("emoji", "🤖"),
                "description": cfg.get("description", ""),
            }

    def list_specialists_desc(self) -> str:
        return "\n".join(
            f"- {k}: {v['description']}" for k, v in self.specialist_meta.items()
        )

    def call(self, agent_name: str, task: str) -> str:
        """Run a specialist in a fresh scratch agent and return its final text output.

        Clones the template's config into a new Agent so this call holds its
        own ``messages`` history. Without this, two concurrent /api/briefing
        requests (or briefing + chat on the same specialist) would race on the
        shared specialist's message list and leak one user's turn into another
        user's context.
        """
        cfg = self._templates.get(agent_name)
        if not cfg:
            return f"[erro] agente '{agent_name}' não encontrado. Disponíveis: {list(self.specialists)}"
        try:
            ag = Agent(
                model=_make_model(),
                tools=cfg["tools"],
                system_prompt=_with_date(cfg["system_prompt"]),
            )
            result = ag(task)
            # Strands Agent() returns an object with .message or similar; handle either
            if hasattr(result, "message"):
                msg = result.message
            else:
                msg = result
            if isinstance(msg, dict):
                # Extract text content
                content = msg.get("content", [])
                texts = []
                for c in content if isinstance(content, list) else [content]:
                    if isinstance(c, dict) and "text" in c:
                        texts.append(c["text"])
                    elif isinstance(c, str):
                        texts.append(c)
                return "\n".join(texts) or str(msg)
            return str(msg)
        except Exception as e:
            return f"[erro ao executar {agent_name}] {e}"


# Build the call_specialist tool that closes over the registry
def build_call_specialist(registry: AgentRegistry):
    @tool
    def call_specialist(agent_name: str, task: str) -> dict:
        """Delega uma tarefa a um agente especialista da plataforma.

        Args:
            agent_name: nome do especialista (ex: 'agenda', 'financeiro', 'pesquisa').
            task: descrição clara do que o especialista deve fazer.

        Retorna a resposta do especialista já pronta para ser mostrada ao usuário.
        """
        output = registry.call(agent_name, task)
        return {"agent": agent_name, "output": output}
    return call_specialist


def build_concierge(registry: AgentRegistry) -> Agent:
    cfg = registry.config["concierge"]
    # Assemble tools: concierge's listed tools + the call_specialist delegator + MCP tools if declared
    tool_fns = [TOOLS_BY_NAME[t] for t in cfg["tools"] if t in TOOLS_BY_NAME]
    tool_fns.append(build_call_specialist(registry))
    tool_fns.extend(load_mcp_tools(cfg.get("mcp_servers")))
    model = _make_model()
    sp = cfg["system_prompt"] + "\n\nEspecialistas disponíveis agora:\n" + registry.list_specialists_desc()
    return Agent(model=model, tools=tool_fns, system_prompt=_with_date(sp))
