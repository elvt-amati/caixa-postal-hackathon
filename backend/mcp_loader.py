"""Wire declared MCP servers into Strands agents at startup.

Each agent entry in agents_config.yaml can list `mcp_servers:` — a list of either:
  - {command: "<cmd>", args: [...], env: {...}}   (stdio transport)
  - {url: "http://host:port/sse"}                  (SSE transport)

At boot the loader spawns an MCPClient per server (globally cached by signature),
then exposes each server's discovered tools to every agent that references it.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_CACHE: dict[str, Any] = {}  # signature -> MCPClient (started)
_TOOLS_CACHE: dict[str, list[Any]] = {}  # signature -> list of MCPAgentTool


def shutdown_all():
    """Stop every cached MCPClient — called on FastAPI shutdown to avoid zombie
    stdio subprocesses across reloads or container restarts (P1.13)."""
    for sig, client in list(_CACHE.items()):
        try:
            if hasattr(client, "stop"):
                client.stop()
        except Exception as e:
            logger.warning(f"mcp stop failed for {sig}: {e}")
        finally:
            _CACHE.pop(sig, None)
            _TOOLS_CACHE.pop(sig, None)


def _mcp_signature(spec: dict) -> str:
    return json.dumps(spec, sort_keys=True, default=str)


def _resolve_env(env: dict | None) -> dict | None:
    """Resolve env placeholders. A value like '${CAIXA_USER_TOKEN}' is substituted
    from the current process env so the caller can opt-in to JWT passthrough:

        mcp_servers:
          - command: "some-mcp"
            env: {AUTH_TOKEN: "${CAIXA_USER_TOKEN}"}

    The main process sets CAIXA_USER_TOKEN per-request via a context var before
    spawning (see auth.user_id_token + registry wiring).
    """
    if not env:
        return None
    out = {}
    for k, v in env.items():
        if isinstance(v, str) and v.startswith("${") and v.endswith("}"):
            envkey = v[2:-1]
            val = os.environ.get(envkey)
            if val is not None:
                out[k] = val
        else:
            out[k] = v
    return out or None


def _make_stdio_transport(command: str, args: list[str], env: dict | None):
    from mcp.client.stdio import stdio_client, StdioServerParameters  # type: ignore

    params = StdioServerParameters(command=command, args=args or [], env=_resolve_env(env))
    return lambda: stdio_client(params)


def _make_sse_transport(url: str, headers: dict | None = None):
    from mcp.client.sse import sse_client  # type: ignore
    # Optional bearer token passthrough via env for SSE MCP servers
    tok = os.environ.get("CAIXA_USER_TOKEN")
    hdrs = dict(headers or {})
    if tok and "Authorization" not in hdrs:
        hdrs["Authorization"] = f"Bearer {tok}"
    if hdrs:
        return lambda: sse_client(url, headers=hdrs)
    return lambda: sse_client(url)


def load_mcp_tools(mcp_servers: list[dict] | None) -> list[Any]:
    """Return a list of Strands-compatible MCP tools for the given server specs.

    Errors on individual servers are logged but don't abort — returned list is
    partial. Each server is started once globally and the tools cached.
    """
    if not mcp_servers:
        return []
    try:
        from strands.tools.mcp import MCPClient
    except Exception as e:
        logger.warning(f"strands.tools.mcp unavailable ({e}) — MCP servers disabled")
        return []

    all_tools: list[Any] = []
    for spec in mcp_servers:
        sig = _mcp_signature(spec)
        if sig in _TOOLS_CACHE:
            all_tools.extend(_TOOLS_CACHE[sig])
            continue
        try:
            if "command" in spec:
                transport = _make_stdio_transport(
                    command=spec["command"], args=spec.get("args", []), env=spec.get("env")
                )
            elif "url" in spec:
                transport = _make_sse_transport(spec["url"])
            else:
                logger.warning(f"skipping MCP spec without command/url: {spec}")
                continue
            client = MCPClient(transport_callable=transport)
            client.start()
            tools = list(client.list_tools_sync())
            logger.info(f"MCP ok: {spec} — loaded {len(tools)} tools: {[t.tool_name for t in tools]}")
            _CACHE[sig] = client
            _TOOLS_CACHE[sig] = tools
            all_tools.extend(tools)
        except Exception as e:
            logger.warning(f"MCP server failed ({spec}): {e}")
            _TOOLS_CACHE[sig] = []  # cache empty so we don't retry constantly
    return all_tools
