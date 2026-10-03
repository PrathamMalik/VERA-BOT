"""In-memory state: pushed contexts (versioned), sent suppression keys, conversations, per-merchant flags.

Everything lives in RAM behind one lock. The judge's test window is ~1 hour, so persistence is not needed —
and the brief asks us to wipe state on /v1/teardown anyway.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict

VALID_SCOPES = ("category", "merchant", "customer", "trigger")


class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.reset()

    def reset(self):
        with self.lock:
            self.contexts: dict[tuple[str, str], dict] = {}   # (scope, id) -> {"version", "payload"}
            self.sent_suppression: set[str] = set()           # suppression keys already used
            self.conversations: dict[str, dict] = {}          # conversation_id -> state
            self.merchant_inbound: dict[str, list[str]] = defaultdict(list)  # merchant_id -> normalized inbound msgs
            self.merchant_flags: dict[str, dict] = defaultdict(dict)        # merchant_id -> {"opted_out": bool, ...}
            self.bodies_sent: dict[str, set[str]] = defaultdict(set)        # conversation -> bodies
            self.last_sent: dict[str, str] = {}                              # merchant_id -> simulated time of last proactive send
            self.replied_since: dict[str, bool] = {}                          # merchant_id -> real reply since last send
            self.rotation: dict[str, int] = defaultdict(int)                  # merchant_id -> scheduled_recurring rotation index
            self.considered: set[str] = set()                                 # suppression keys deliberately skipped
            self.started = time.time()

    # ------------------------------------------------------------ contexts
    def put_context(self, scope: str, cid: str, version: int, payload: dict):
        """Returns (accepted, current_version). Same version = idempotent no-op (accepted)."""
        with self.lock:
            key = (scope, cid)
            cur = self.contexts.get(key)
            if cur is not None:
                if version < cur["version"]:
                    return False, cur["version"]
                if version == cur["version"]:
                    return True, cur["version"]  # idempotent re-post
            if scope == "category" and cur is not None and isinstance(payload, dict):
                # mid-test context injection: mark research items that are new in this version (decision: newest relevant, with floor)
                old_ids = {d.get("id") for d in (cur["payload"].get("digest") or []) if isinstance(d, dict)}
                for d in payload.get("digest") or []:
                    if isinstance(d, dict) and d.get("id") not in old_ids:
                        d["_new"] = True
            self.contexts[key] = {"version": version, "payload": payload}
            return True, version

    def get(self, scope: str, cid: str | None) -> dict | None:
        if not cid:
            return None
        with self.lock:
            item = self.contexts.get((scope, cid))
            return item["payload"] if item else None

    def counts(self) -> dict:
        out = {s: 0 for s in VALID_SCOPES}
        with self.lock:
            for (scope, _cid) in self.contexts:
                out[scope] = out.get(scope, 0) + 1
        return out

    def merchants(self) -> list[dict]:
        with self.lock:
            return [v["payload"] for (s, _), v in self.contexts.items() if s == "merchant"]

    def mark_reply(self, merchant_id):
        with self.lock:
            self.replied_since[merchant_id] = True

    def category_for(self, merchant: dict | None) -> dict | None:
        if not merchant:
            return None
        return self.get("category", merchant.get("category_slug"))

    def customers_of(self, merchant_id: str) -> list[dict]:
        with self.lock:
            return [v["payload"] for (s, _), v in self.contexts.items()
                    if s == "customer" and v["payload"].get("merchant_id") == merchant_id]

    # ------------------------------------------------------------ conversations
    def conv(self, conversation_id: str) -> dict | None:
        with self.lock:
            return self.conversations.get(conversation_id)

    def new_conv(self, conversation_id: str, **fields) -> dict:
        with self.lock:
            st = {
                "conversation_id": conversation_id,
                "merchant_id": fields.get("merchant_id"),
                "customer_id": fields.get("customer_id"),
                "trigger_id": fields.get("trigger_id"),
                "send_as": fields.get("send_as", "vera"),
                "kind": fields.get("kind"),
                "offer": fields.get("offer"),      # what the bot offered to do (drives action mode)
                "turns": [],                        # {"from": "bot"|"merchant"|"customer", "body": str}
                "auto_reply_count": 0,
                "stage": "pitch",                   # pitch -> action -> closed
                "ended": False,
            }
            self.conversations[conversation_id] = st
            return st
