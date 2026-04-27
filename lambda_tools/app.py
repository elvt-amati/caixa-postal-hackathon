"""Consolidated Lambda behind AgentCore Gateway.

Every Smithy operation routes to this single function. The operation name
comes from the event's path or 'operation' parameter. User identity is
either (a) supplied as `user_id` in the input, or (b) extracted from the
Cognito claims that Gateway forwards in `requestContext.authorizer`.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from decimal import Decimal
from typing import Any

import boto3

REGION = os.environ.get("AWS_REGION", "us-east-1")
TABLE = os.environ.get("CAIXA_TABLE", "caixa-items")
SES_FROM = os.environ.get("SES_FROM", "")

_dyn = boto3.resource("dynamodb", region_name=REGION)
_table = _dyn.Table(TABLE)
_ses = boto3.client("ses", region_name=REGION)


# ---------------------------------------------------------------------------
# JSON/decimal helpers
# ---------------------------------------------------------------------------

def _decimal(v: Any) -> Any:
    if isinstance(v, float):
        return Decimal(str(v))
    if isinstance(v, dict):
        return {k: _decimal(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_decimal(x) for x in v]
    return v


def _jsonable(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v) if v % 1 else int(v)
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_jsonable(x) for x in v]
    return v


# ---------------------------------------------------------------------------
# DynamoDB operations
# ---------------------------------------------------------------------------

_PROTECTED = {"pk", "sk", "category", "created_at", "deleted_at", "user_id"}

CANONICAL = {
    "task":     ["title", "notes", "due_date", "status"],
    "payment":  ["description", "amount", "due_date", "payee", "status"],
    "reminder": ["title", "at", "notes"],
    "contact":  ["name", "phone", "email", "notes"],
    "note":     ["title", "body"],
    "email":    ["to", "subject", "body", "status"],
}


def _pk(cat: str, uid_: str) -> str:
    return f"CAT#{uid_}#{cat}"


def _put(cat: str, uid_: str, **fields) -> dict:
    item = {
        "pk": _pk(cat, uid_),
        "sk": f"TS#{int(time.time() * 1000)}#{uuid.uuid4().hex[:8]}",
        "category": cat,
        "user_id": uid_,
        "created_at": int(time.time()),
        **{k: v for k, v in fields.items() if v is not None},
    }
    _table.put_item(Item=_decimal(item))
    return _jsonable(item)


def _list(cat: str, uid_: str, limit: int = 20) -> list[dict]:
    resp = _table.query(
        KeyConditionExpression="pk = :pk",
        ExpressionAttributeValues={":pk": _pk(cat, uid_)},
        FilterExpression="attribute_not_exists(deleted_at)",
        ScanIndexForward=False,
        Limit=limit,
    )
    return [_jsonable(x) for x in resp.get("Items", [])]


def _update(cat: str, uid_: str, sk: str, fields: dict) -> dict | None:
    valid = {k: v for k, v in (fields or {}).items()
             if k not in _PROTECTED and k in CANONICAL.get(cat, [])}
    if not valid:
        return None
    pieces, names, values = [], {"#updated_at": "updated_at"}, {":now": int(time.time())}
    for k, v in valid.items():
        names[f"#{k}"] = k
        values[f":{k}"] = _decimal(v)
        pieces.append(f"#{k} = :{k}")
    pieces.append("#updated_at = :now")
    resp = _table.update_item(
        Key={"pk": _pk(cat, uid_), "sk": sk},
        UpdateExpression="SET " + ", ".join(pieces),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
        ReturnValues="ALL_NEW",
    )
    return _jsonable(resp.get("Attributes"))


def _delete(cat: str, uid_: str, sk: str, hard: bool = False) -> dict:
    if hard:
        _table.delete_item(Key={"pk": _pk(cat, uid_), "sk": sk})
        return {"ok": True, "hard": True, "id": sk}
    _table.update_item(
        Key={"pk": _pk(cat, uid_), "sk": sk},
        UpdateExpression="SET #d = :n, #s = :st, #u = :n",
        ExpressionAttributeNames={"#d": "deleted_at", "#s": "status", "#u": "updated_at"},
        ExpressionAttributeValues={":n": int(time.time()), ":st": "deleted"},
    )
    return {"ok": True, "id": sk, "hard": False}


def _undo(cat: str, uid_: str, sk: str) -> dict | None:
    _table.update_item(
        Key={"pk": _pk(cat, uid_), "sk": sk},
        UpdateExpression="REMOVE #d SET #s = :o, #u = :n",
        ExpressionAttributeNames={"#d": "deleted_at", "#s": "status", "#u": "updated_at"},
        ExpressionAttributeValues={":o": "open", ":n": int(time.time())},
    )
    resp = _table.get_item(Key={"pk": _pk(cat, uid_), "sk": sk})
    return _jsonable(resp.get("Item"))


def _send_email(to: str, subject: str, body: str, uid_: str) -> dict:
    if not SES_FROM:
        return {"ok": False, "message": "SES_FROM não configurado"}
    try:
        _ses.send_email(
            Source=SES_FROM,
            Destination={"ToAddresses": [to]},
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {"Text": {"Data": body, "Charset": "UTF-8"}},
            },
        )
        _put("email", uid_, to=to, subject=subject, body=body[:500], status="sent")
        return {"ok": True, "message": f"email enviado para {to}"}
    except Exception as e:
        _put("email", uid_, to=to, subject=subject, body=body[:500], status=f"error: {e}")
        return {"ok": False, "message": f"falha: {e}"}


# ---------------------------------------------------------------------------
# Identity extraction
# ---------------------------------------------------------------------------

def _resolve_user(event: dict, body: dict) -> str:
    # SECURITY: verified Cognito claims forwarded by the Gateway ALWAYS win over
    # the body-supplied user_id. Otherwise any authenticated caller could pass
    # {"user_id": "<victim>"} and read/mutate another tenant's items.
    # The body hint is only used when no JWT is forwarded (e.g., local/dev).
    try:
        claims = (event.get("requestContext", {})
                  .get("authorizer", {})
                  .get("jwt", {})
                  .get("claims", {}))
        sub = claims.get("sub") or claims.get("email")
        if sub:
            return sub
    except Exception:
        pass
    # AgentCore Gateway also forwards the caller identity via client_context
    # under `context.client_context.custom`. When running behind Gateway with
    # Cognito JWT auth, the verified client_id or sub lands there.
    # NOTE: the `context` object is not in this function's scope — the handler
    # prefers the explicit body hint only when no trusted source is present.
    hint = body.get("user_id") or body.get("userId")
    if hint:
        return str(hint)
    return "anon"


# ---------------------------------------------------------------------------
# Operation dispatcher
# ---------------------------------------------------------------------------

def _op_from_event(event: dict) -> str:
    # AgentCore Gateway Lambda target passes the tool name in several forms across versions.
    # Scan the most likely spots before giving up.
    candidates = [
        event.get("operation"),
        event.get("path"),
        event.get("resource"),
        event.get("toolName"),
        event.get("tool_name"),
        event.get("name"),
        event.get("mcp", {}).get("toolName") if isinstance(event.get("mcp"), dict) else None,
    ]
    # Also check requestContext and context-style wrappers
    rc = event.get("requestContext") or {}
    if isinstance(rc, dict):
        candidates.append(rc.get("operationName"))
        candidates.append(rc.get("resourcePath"))
    for c in candidates:
        if c:
            return str(c).strip("/").lower().replace("caixa-tools___", "")
    return ""


def handler(event: dict, context) -> dict:
    # AgentCore Gateway Lambda target contract (discovered empirically):
    # - Tool name lives in context.client_context.custom["bedrockAgentCoreToolName"]
    #   (prefixed with target name, e.g. "caixa-tools___create_task")
    # - Arguments are the event dict itself (flat top-level).
    op = ""
    try:
        custom = getattr(context.client_context, "custom", {}) or {}
        raw = custom.get("bedrockAgentCoreToolName", "")
        op = raw.split("___", 1)[-1].lower() if raw else ""
    except Exception:
        pass

    body = event if isinstance(event, dict) else {}
    # Fallback: legacy API Gateway-style shape
    if "body" in body and isinstance(body["body"], str):
        try:
            body = json.loads(body["body"] or "{}")
        except Exception:
            pass
    if not op:
        op = _op_from_event(event).lower()

    uid_ = _resolve_user(event, body)

    try:
        result = dispatch(op, uid_, body)
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(_jsonable(result)),
        }
    except KeyError as e:
        return {"statusCode": 400, "body": json.dumps({"error": f"faltou campo: {e}"})}
    except Exception as e:
        return {"statusCode": 500, "body": json.dumps({"error": str(e)})}


def dispatch(op: str, uid_: str, body: dict) -> Any:
    # Write operations ---
    if op in ("create_task", "tasks"):
        return _put("task", uid_, title=body["title"], notes=body.get("notes"),
                    due_date=body.get("due_date"), status="open")
    if op in ("create_payment", "payments"):
        return _put("payment", uid_, description=body["description"],
                    amount=float(body["amount"]), due_date=body.get("due_date"),
                    payee=body.get("payee"))
    if op in ("create_reminder", "reminders"):
        return _put("reminder", uid_, title=body["title"], at=body["at"],
                    notes=body.get("notes"))
    if op in ("save_contact", "contacts"):
        return _put("contact", uid_, name=body["name"], phone=body.get("phone"),
                    email=body.get("email"), notes=body.get("notes"))
    if op in ("save_note", "notes"):
        return _put("note", uid_, title=body["title"], body=body["body"])
    if op == "send_email":
        return _send_email(body["to"], body["subject"], body["body"], uid_)

    # Read operations ---
    if op == "list_tasks":     return _list("task", uid_, int(body.get("limit", 20)))
    if op == "list_payments":  return _list("payment", uid_, int(body.get("limit", 20)))
    if op == "list_reminders": return _list("reminder", uid_, int(body.get("limit", 20)))
    if op == "list_contacts":  return _list("contact", uid_, int(body.get("limit", 20)))
    if op == "list_notes":     return _list("note", uid_, int(body.get("limit", 20)))

    # Mutation by id ---
    if op == "update_item":
        cat = body["category"]
        r = _update(cat, uid_, body["id"], body.get("fields", {}))
        return {"ok": bool(r), "item": r} if r else {"ok": False, "message": "item não encontrado"}
    if op == "delete_item":
        return _delete(body["category"], uid_, body["id"], hard=body.get("hard", False))
    if op == "undo_delete":
        r = _undo(body["category"], uid_, body["id"])
        return {"ok": bool(r), "item": r}

    # Generative-UI passthrough ---
    if op == "render_chart":
        return {"ok": True, "chart_type": body.get("chart_type", "bar"),
                "title": body.get("title", ""), "labels": body.get("labels", []),
                "values": [float(v) for v in body.get("values", [])],
                "unit": body.get("unit", "")}
    if op == "publish_card":
        return {"ok": True, "published_by": body.get("published_by"),
                "agent": body.get("published_by"),
                "key": body["key"],
                "card": {"template": body["template"], "title": body["title"],
                         "spec": body.get("spec", {}), "updated_at": int(time.time())}}
    if op == "search_web":
        q = body.get("query", "")
        return [{"title": f"demo '{q}'", "url": "https://example.com/1",
                 "snippet": "resultado sintético — plugue um MCP real."}]

    raise ValueError(f"operação desconhecida: {op}")
