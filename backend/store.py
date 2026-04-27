"""DynamoDB wrapper for the caixa-postal universal inbox.

P0.3: all CAT items are user-scoped via `pk=CAT#<user_id>#<category>`. The
active user id is read from `current_user_id` ContextVar, set by the FastAPI
middleware. Tools don't need to pass user_id — they inherit it from the request
context. When no user is bound (e.g., a CLI script), defaults to "anon".
"""
from __future__ import annotations

import os
import time
import uuid
from contextvars import ContextVar
from decimal import Decimal
from typing import Any

import boto3

REGION = os.environ.get("AWS_REGION", "us-east-1")
TABLE = os.environ.get("CAIXA_TABLE", "caixa-items")

_dyn = boto3.resource("dynamodb", region_name=REGION)
_table = _dyn.Table(TABLE)

# Set by main.py middleware for each request. Tools read via active_user_id().
current_user_id: ContextVar[str] = ContextVar("current_user_id", default="anon")


def active_user_id() -> str:
    return current_user_id.get()


def _cat_pk(category: str, user_id: str | None = None) -> str:
    uid_ = user_id or active_user_id()
    return f"CAT#{uid_}#{category}"


def _decimal(v: Any) -> Any:
    if isinstance(v, float):
        return Decimal(str(v))
    if isinstance(v, dict):
        return {k: _decimal(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_decimal(x) for x in v]
    return v


def _json_safe(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v) if v % 1 else int(v)
    if isinstance(v, dict):
        return {k: _json_safe(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_json_safe(x) for x in v]
    return v


def put_item(category: str, user_id: str | None = None, **fields) -> dict:
    """Insert one item. Returns the stored item (json-safe). Scopes to active user
    unless user_id is passed explicitly (e.g. for ops/admin tooling)."""
    uid_ = user_id or active_user_id()
    item = {
        "pk": _cat_pk(category, uid_),
        "sk": f"TS#{int(time.time() * 1000)}#{uuid.uuid4().hex[:8]}",
        "category": category,
        "user_id": uid_,
        "created_at": int(time.time()),
        **{k: v for k, v in fields.items() if v is not None},
    }
    _table.put_item(Item=_decimal(item))
    return _json_safe(item)


def list_by_category(
    category: str,
    limit: int = 50,
    include_deleted: bool = False,
    user_id: str | None = None,
) -> list[dict]:
    # P1.7: push the soft-delete filter into the query instead of overfetching 2×.
    kwargs: dict[str, Any] = {
        "KeyConditionExpression": "pk = :pk",
        "ExpressionAttributeValues": {":pk": _cat_pk(category, user_id)},
        "ScanIndexForward": False,
        "Limit": limit,
    }
    if not include_deleted:
        kwargs["FilterExpression"] = "attribute_not_exists(deleted_at)"
    resp = _table.query(**kwargs)
    items = [_json_safe(x) for x in resp.get("Items", [])]
    return items[:limit]


def get_item(category: str, sk: str, user_id: str | None = None) -> dict | None:
    resp = _table.get_item(Key={"pk": _cat_pk(category, user_id), "sk": sk})
    it = resp.get("Item")
    return _json_safe(it) if it else None


_PROTECTED_FIELDS = {"pk", "sk", "category", "created_at", "deleted_at", "user_id"}


def update_item(
    category: str,
    sk: str,
    fields: dict,
    user_id: str | None = None,
    _internal_fields: dict | None = None,
) -> dict | None:
    """Generic update. Builds SET expression with ExpressionAttributeNames
    to handle reserved words (amount, status, name, ...). Always writes updated_at.
    Protected fields (pk/sk/category/created_at/deleted_at/user_id) are always
    rejected from caller-supplied `fields` to prevent privilege-escalation.
    Internal callers (delete/undo) use `_internal_fields` to set protected keys.
    """
    pieces = []
    names = {}
    values = {":now": int(time.time())}
    merged = {**(fields or {}), **(_internal_fields or {})}
    for k, v in merged.items():
        if v is None:
            continue
        if k in _PROTECTED_FIELDS and (not _internal_fields or k not in _internal_fields):
            continue
        alias_n = f"#{k}"
        alias_v = f":{k}"
        names[alias_n] = k
        values[alias_v] = _decimal(v)
        pieces.append(f"{alias_n} = {alias_v}")
    pieces.append("#updated_at = :now")
    names["#updated_at"] = "updated_at"
    resp = _table.update_item(
        Key={"pk": _cat_pk(category, user_id), "sk": sk},
        UpdateExpression="SET " + ", ".join(pieces),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
        ReturnValues="ALL_NEW",
    )
    return _json_safe(resp.get("Attributes"))


def delete_item(category: str, sk: str, hard: bool = False, user_id: str | None = None) -> dict:
    """Soft-delete by default (marks deleted_at). hard=True removes the record."""
    if hard:
        _table.delete_item(Key={"pk": _cat_pk(category, user_id), "sk": sk})
        return {"ok": True, "hard": True, "id": sk}
    updated = update_item(
        category, sk, fields={},
        _internal_fields={"deleted_at": int(time.time()), "status": "deleted"},
        user_id=user_id,
    )
    return {"ok": True, "hard": False, "id": sk, "item": updated}


def undo_delete(category: str, sk: str, user_id: str | None = None) -> dict | None:
    """Reverse a soft-delete. Clears deleted_at and restores status='open'."""
    it = get_item(category, sk, user_id=user_id)
    if not it:
        return None
    _table.update_item(
        Key={"pk": _cat_pk(category, user_id), "sk": sk},
        UpdateExpression="REMOVE #d SET #s = :open, #u = :now",
        ExpressionAttributeNames={"#d": "deleted_at", "#s": "status", "#u": "updated_at"},
        ExpressionAttributeValues={":open": "open", ":now": int(time.time())},
    )
    return get_item(category, sk, user_id=user_id)


def list_all(limit: int = 200) -> list[dict]:
    resp = _table.scan(Limit=limit)
    items = sorted(resp.get("Items", []), key=lambda x: x.get("created_at", 0), reverse=True)
    return [_json_safe(x) for x in items]


# -----------------------------------------------------------------------------
# Thread persistence — per-user, per-agent chat history
# pk=THREAD#<user_id>#<agent>, sk=MSG#<ts_ms>#<uuid>
# -----------------------------------------------------------------------------


def save_thread_message(user_id: str, agent: str, role: str, **fields) -> dict:
    item = {
        "pk": f"THREAD#{user_id}#{agent}",
        "sk": f"MSG#{int(time.time() * 1000)}#{uuid.uuid4().hex[:6]}",
        "user_id": user_id,
        "agent": agent,
        "role": role,
        "created_at": int(time.time()),
        **{k: v for k, v in fields.items() if v is not None},
    }
    _table.put_item(Item=_decimal(item))
    return _json_safe(item)


def load_thread_messages(user_id: str, agent: str, limit: int = 200) -> list[dict]:
    resp = _table.query(
        KeyConditionExpression="pk = :pk",
        ExpressionAttributeValues={":pk": f"THREAD#{user_id}#{agent}"},
        ScanIndexForward=True,  # oldest → newest
        Limit=limit,
    )
    return [_json_safe(x) for x in resp.get("Items", [])]


def clear_thread(user_id: str, agent: str) -> int:
    n = 0
    resp = _table.query(
        KeyConditionExpression="pk = :pk",
        ExpressionAttributeValues={":pk": f"THREAD#{user_id}#{agent}"},
    )
    with _table.batch_writer() as w:
        for it in resp.get("Items", []):
            w.delete_item(Key={"pk": it["pk"], "sk": it["sk"]})
            n += 1
    return n


def clear_user(user_id: str | None = None) -> int:
    """Delete every CAT item owned by the active user (or the supplied user_id).

    Replaces the old table-wide ``clear_all`` — that one scanned the whole
    table and would let any caller wipe other users' data. Now the caller's
    blast radius is limited to their own partition keys.
    """
    uid_ = user_id or active_user_id()
    n = 0
    # Iterate known categories so we only touch pk=CAT#<uid>#<cat>. Any CARD/
    # THREAD rows for this user are left alone on purpose — ops/reset is
    # meant to clean up demo data, not logins.
    cats = ["task", "payment", "reminder", "contact", "note", "email", "pitch"]
    for cat in cats:
        pk = _cat_pk(cat, uid_)
        resp = _table.query(
            KeyConditionExpression="pk = :pk",
            ExpressionAttributeValues={":pk": pk},
        )
        with _table.batch_writer() as w:
            for it in resp.get("Items", []):
                w.delete_item(Key={"pk": it["pk"], "sk": it["sk"]})
                n += 1
    return n


# ---------------------------------------------------------------------------
# Event chat — public room + per-pair DMs.
# ---------------------------------------------------------------------------
# Shared across every visitor on the canonical CloudFront domain (the
# participant forks redirect /desafio et al to us, but /chat + /api/chat
# only exist on our stack so chat is always global for the event).
#
# One partition per "thread" keeps queries cheap:
#   pk=CHAT#PUBLIC                               public room
#   pk=CHAT#DM#<a>#<b>   where a < b             per-pair DM thread
# Sort keys are TS#<ms>#<rand> so paginated queries by ``since`` cursor
# return everything newer than the client's last seen timestamp.
# Every item carries a ``ttl`` 48h out; DynamoDB sweeps them after the event.

_CHAT_TTL_SECS = 48 * 3600


def _dm_pk(id_a: str, id_b: str) -> str:
    """DM partition key from two guest cookie IDs (not display names).

    IDs are sorted so `dm_pk(alice_id, bob_id) == dm_pk(bob_id, alice_id)`
    — one partition per pair regardless of who initiated.
    """
    lo, hi = sorted([id_a, id_b])
    return f"CHAT#DM#{lo}#{hi}"


def save_chat_public(guest_id: str, name: str, text: str, image: str | None = None) -> dict:
    now_ms = int(time.time() * 1000)
    item = {
        "pk": "CHAT#PUBLIC",
        "sk": f"TS#{now_ms:013d}#{uuid.uuid4().hex[:6]}",
        "guest_id": guest_id,
        "name": name,
        "text": text,
        "ts": now_ms,
        "ttl": int(time.time()) + _CHAT_TTL_SECS,
    }
    if image:
        item["image"] = image
    _table.put_item(Item=_decimal(item))
    return _json_safe(item)


def load_chat_public(since_ms: int = 0, limit: int = 200) -> list[dict]:
    kce = "pk = :pk"
    eav: dict = {":pk": "CHAT#PUBLIC"}
    if since_ms:
        kce += " AND sk > :sk"
        eav[":sk"] = f"TS#{since_ms:013d}#￿"
    resp = _table.query(
        KeyConditionExpression=kce,
        ExpressionAttributeValues=eav,
        ScanIndexForward=True,
        Limit=limit,
    )
    return [_json_safe(x) for x in resp.get("Items", [])]


def save_chat_dm(sender_id: str, sender_name: str,
                  recipient_id: str, recipient_name: str,
                  text: str, image: str | None = None) -> dict:
    """DMs are keyed on guest cookie IDs, not display names."""
    now_ms = int(time.time() * 1000)
    item = {
        "pk": _dm_pk(sender_id, recipient_id),
        "sk": f"TS#{now_ms:013d}#{uuid.uuid4().hex[:6]}",
        "from_id": sender_id,
        "from_name": sender_name,
        "to_id": recipient_id,
        "to_name": recipient_name,
        "text": text,
        "ts": now_ms,
        "ttl": int(time.time()) + _CHAT_TTL_SECS,
    }
    if image:
        item["image"] = image
    _table.put_item(Item=_decimal(item))
    return _json_safe(item)


def load_chat_dm(my_id: str, peer_id: str, since_ms: int = 0, limit: int = 200) -> list[dict]:
    kce = "pk = :pk"
    eav: dict = {":pk": _dm_pk(my_id, peer_id)}
    if since_ms:
        kce += " AND sk > :sk"
        eav[":sk"] = f"TS#{since_ms:013d}#￿"
    resp = _table.query(
        KeyConditionExpression=kce,
        ExpressionAttributeValues=eav,
        ScanIndexForward=True,
        Limit=limit,
    )
    return [_json_safe(x) for x in resp.get("Items", [])]
