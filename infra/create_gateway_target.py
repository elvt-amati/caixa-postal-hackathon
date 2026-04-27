"""Register the Lambda MCP target on our AgentCore Gateway, driven by the
canonical Smithy model at ``infra/smithy/caixa-tools.json``.

Flow:
  1. Load the Smithy 2.0 JSON AST.
  2. Walk the service's operations. For each one, derive the ``name`` (from
     the HTTP URI if present, else camelCase->snake_case of the operation
     shape name), ``description`` (``smithy.api#documentation``), and an
     ``inputSchema`` (walk the input structure's members and map Smithy
     types to JSON Schema types).
  3. Pass the resulting list as the Lambda target's
     ``toolSchema.inlinePayload`` — which is the only shape Gateway's
     ``mcp.lambda`` target accepts today.

So Smithy is the source of truth for tool contracts; Lambda is the
execution path; the JSON-Schema ``inlinePayload`` is generated at
registration time and never hand-maintained.

Re-runnable: any target with the same name is deleted first.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import boto3

TARGET_NAME = "caixa-tools"
SMITHY_PATH = Path(__file__).parent / "smithy" / "caixa-tools.json"

ctrl = boto3.client("bedrock-agentcore-control", region_name="us-east-1")
cfn = boto3.client("cloudformation", region_name="us-east-1")

exports = {e["Name"]: e["Value"] for e in cfn.list_exports()["Exports"]}
lambda_arn = exports["CaixaToolsFunctionArn"]
gw_id = exports["CaixaGatewayId"]


# ---------------------------------------------------------------------------
# Smithy -> JSON Schema
# ---------------------------------------------------------------------------

_SMITHY_SCALAR = {
    "smithy.api#String":  "string",
    "smithy.api#Boolean": "boolean",
    "smithy.api#Integer": "integer",
    "smithy.api#Long":    "integer",
    "smithy.api#Float":   "number",
    "smithy.api#Double":  "number",
    "smithy.api#Byte":    "integer",
    "smithy.api#Short":   "integer",
    "smithy.api#Timestamp": "string",
    "smithy.api#Document":  "object",
    "smithy.api#Blob":      "string",
}


def _camel_to_snake(name: str) -> str:
    """Fallback tool name when the operation has no @http(uri=/name) trait."""
    s1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1).lower()


def _shape_to_json_schema(shape: dict, shapes: dict) -> dict:
    """Recursively map a Smithy shape (by its already-resolved dict) to JSON Schema."""
    t = shape.get("type", "string")
    if t == "structure":
        props: dict = {}
        required: list[str] = []
        for mname, member in (shape.get("members") or {}).items():
            target = member.get("target")
            target_shape = shapes.get(target) if target else None
            if target in _SMITHY_SCALAR:
                sub = {"type": _SMITHY_SCALAR[target]}
            elif target_shape:
                sub = _shape_to_json_schema(target_shape, shapes)
            else:
                sub = {"type": "string"}
            doc = member.get("traits", {}).get("smithy.api#documentation")
            if doc:
                sub["description"] = doc
            props[mname] = sub
            if member.get("traits", {}).get("smithy.api#required") is not None:
                required.append(mname)
        out: dict = {"type": "object", "properties": props}
        if required:
            out["required"] = required
        return out
    if t == "list":
        item = shape.get("member", {}).get("target")
        item_shape = shapes.get(item) if item else None
        if item in _SMITHY_SCALAR:
            item_schema: dict = {"type": _SMITHY_SCALAR[item]}
        elif item_shape:
            item_schema = _shape_to_json_schema(item_shape, shapes)
        else:
            item_schema = {"type": "string"}
        return {"type": "array", "items": item_schema}
    # scalar
    if shape.get("type") in _SMITHY_SCALAR:
        return {"type": _SMITHY_SCALAR[shape["type"]]}
    return {"type": _SMITHY_SCALAR.get(t, "string")}


def _derive_tool(op_id: str, shapes: dict) -> dict:
    op = shapes[op_id]
    traits = op.get("traits", {})
    http = traits.get("smithy.api#http") or {}
    uri = http.get("uri", "")
    if uri and uri.startswith("/"):
        name = uri[1:]
    else:
        name = _camel_to_snake(op_id.split("#")[-1])
    description = traits.get("smithy.api#documentation", name)

    input_id = op.get("input", {}).get("target")
    input_shape = shapes.get(input_id) if input_id else None
    if input_shape and input_shape.get("type") == "structure":
        input_schema = _shape_to_json_schema(input_shape, shapes)
    else:
        input_schema = {"type": "object", "properties": {}, "required": []}

    return {
        "name": name,
        "description": description,
        "inputSchema": input_schema,
        "outputSchema": {
            "type": "object",
            "properties": {"result": {"type": "object"}},
        },
    }


def load_tools_from_smithy() -> list[dict]:
    doc = json.loads(SMITHY_PATH.read_text())
    shapes = doc["shapes"]
    service = next(
        s for s in shapes.values()
        if s.get("type") == "service"
    )
    op_ids = [o["target"] for o in service.get("operations", [])]
    return [_derive_tool(oid, shapes) for oid in op_ids]


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------

TOOLS = load_tools_from_smithy()

# delete any stale target with the same name, then poll until it's gone so
# the create call doesn't race with the background deletion.
for t in ctrl.list_gateway_targets(gatewayIdentifier=gw_id).get("items", []):
    if t.get("name") == TARGET_NAME:
        print(f"deleting stale target {t['targetId']}")
        ctrl.delete_gateway_target(gatewayIdentifier=gw_id, targetId=t["targetId"])
for _ in range(30):
    names = {t.get("name") for t in
             ctrl.list_gateway_targets(gatewayIdentifier=gw_id).get("items", [])}
    if TARGET_NAME not in names:
        break
    time.sleep(2)

print(f"creating target on gateway {gw_id} -> {lambda_arn}  ({len(TOOLS)} tools, from Smithy)")
resp = ctrl.create_gateway_target(
    gatewayIdentifier=gw_id,
    name=TARGET_NAME,
    description="Caixa Postal tools — Lambda invoked by AgentCore Gateway, contracts from Smithy 2.0.",
    targetConfiguration={
        "mcp": {
            "lambda": {
                "lambdaArn": lambda_arn,
                "toolSchema": {"inlinePayload": TOOLS},
            }
        }
    },
    credentialProviderConfigurations=[
        {"credentialProviderType": "GATEWAY_IAM_ROLE"}
    ],
)
print(json.dumps(resp, indent=2, default=str)[:800])
