"""Invite links so friends can add their Amazon order history without a login.

Each invite is a random code tied to a label you choose ("jay", "mom"). Whatever a friend sends is filed
under that label, so they can't write as anyone else, and you can revoke one link without touching the others.
An invite can only ADD orders; it can't read anything.
"""

from __future__ import annotations

import json
import secrets

from . import db

KEY = "invites"


def _all(conn) -> dict:
    return json.loads(db.get_setting(conn, KEY) or "{}")


def _save(conn, data: dict) -> None:
    db.set_setting(conn, KEY, json.dumps(data))


def create(conn, label: str) -> str:
    label = "".join(ch for ch in (label or "").strip().lower() if ch.isalnum() or ch in "-_")[:30] or "friend"
    code = secrets.token_urlsafe(9)
    data = _all(conn)
    data[code] = {"label": label, "created": db.now()}
    _save(conn, data)
    return code


def label_for(conn, code: str | None) -> str | None:
    if not code:
        return None
    for k, v in _all(conn).items():
        if secrets.compare_digest(k, code):
            return v["label"]
    return None


def revoke(conn, code: str) -> bool:
    data = _all(conn)
    found = data.pop(code, None) is not None
    _save(conn, data)
    return found


def listing(conn) -> list[dict]:
    counts = dict(conn.execute("SELECT buyer, COUNT(*) FROM orders GROUP BY buyer").fetchall())
    return [{"code": k, "label": v["label"], "created": v["created"], "orders": counts.get(v["label"], 0)}
            for k, v in _all(conn).items()]
