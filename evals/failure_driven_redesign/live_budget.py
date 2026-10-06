"""Opt-in shared API budget. No request without a saved explicit authorization.

Peak CNY prices frozen from official DeepSeek pricing on 2026-09-16:
input cache miss 2 / million, output 8 / million. Reserve twice those rates;
never refund uncertain requests. This is a conservative bound, not an invoice.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import math
import os
import sqlite3
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from agent.llm.client import LLMCall


class BudgetStop(RuntimeError):
    pass


class BudgetLedger:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.authorization = json.loads((self.folder / "AUTHORIZATION.json").read_text(encoding="utf-8"))
        self._authorization_hash = self._auth_hash(self.authorization)
        self._validate_authorization(self.authorization)
        self.path = self.folder / "budget.sqlite"
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS calls (id TEXT PRIMARY KEY, batch TEXT, status TEXT, reserved REAL, charged REAL, input_bound INTEGER, output_bound INTEGER, usage TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS flags (name TEXT PRIMARY KEY, value TEXT)")
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT value FROM flags WHERE name='authorization_hash'").fetchone()
            if prior and prior[0] != self._authorization_hash:
                raise BudgetStop("authorization_changed_for_existing_ledger")
            if not prior:
                if db.execute("SELECT count(*) FROM calls").fetchone()[0]:
                    raise BudgetStop("unbound_existing_ledger")
                db.execute("INSERT INTO flags VALUES ('authorization_hash',?)", (self._authorization_hash,))

    @staticmethod
    def _auth_hash(auth):
        return hashlib.sha256(json.dumps(auth, sort_keys=True, allow_nan=False).encode()).hexdigest()

    @staticmethod
    def _validate_authorization(auth):
        if auth.get("approved") is not True or not isinstance(auth.get("user_authorization"), str) or not auth["user_authorization"].strip():
            raise BudgetStop("paid_budget_not_authorized")
        for name in ("max_calls", "max_concurrency", "max_input_bound_per_call"):
            if type(auth.get(name)) is not int or auth[name] <= 0:
                raise BudgetStop("invalid_authorization_limits")
        remaining = auth.get("remaining_budget_authorization")
        continuation = auth.get("continuation_authorization")
        if remaining is not None and continuation is not None:
            raise BudgetStop("ambiguous_budget_authorization")
        allowed_cny = 15
        if remaining is not None:
            if (not isinstance(remaining, dict) or remaining.get("approved") is not True
                    or remaining.get("user_response") != "预算还有16元"
                    or remaining.get("date") != "2026-09-20"
                    or remaining.get("remaining_cny") != 16
                    or type(remaining.get("base_calls")) is not int or remaining["base_calls"] < 0
                    or type(remaining.get("base_cny")) not in (int, float)
                    or not math.isfinite(remaining["base_cny"]) or remaining["base_cny"] < 0
                    or auth["max_calls"] > remaining["base_calls"] + 800):
                raise BudgetStop("remaining_budget_not_authorized")
            allowed_cny = remaining["base_cny"] + 16
        elif continuation is not None:
            if (not isinstance(continuation, dict) or continuation.get("approved") is not True
                    or continuation.get("user_response") != auth["user_authorization"]
                    or continuation.get("date") != "2026-09-30"
                    or continuation.get("remaining_cny") != 13
                    or continuation.get("separate_ledger") is not True
                    or auth["max_calls"] > 2000):
                raise BudgetStop("continuation_budget_not_authorized")
            allowed_cny = continuation["remaining_cny"]
        if (type(auth.get("max_cny")) not in (int, float) or not math.isfinite(auth["max_cny"])
                or not 0 < auth["max_cny"] <= allowed_cny + 1e-8 or auth["max_concurrency"] > 3):
            raise BudgetStop("invalid_authorization_limits")
        if remaining is None and continuation is None and auth["max_cny"] > 10 and auth.get("extension_user_authorization") != "如果执行不完，再保留5元费用的可用":
            raise BudgetStop("additional_budget_not_authorized")

    def activate_extension(self):
        """Apply the user's extra 5 CNY between batches, preserving every charge.

        The authorization/ledger hash is intentionally changed. Previously opened
        clients must stop and reopen, never silently acquire a larger allowance.
        """
        extension = json.loads((self.folder / "BUDGET_EXTENSION_AUTHORIZATION.json").read_text(encoding="utf-8"))
        if (extension.get("approved") is not True or extension.get("original_max_cny") != 10
                or extension.get("extended_max_cny") != 15 or extension.get("additional_cny") != 5
                or extension.get("user_response") != "如果执行不完，再保留5元费用的可用"):
            raise BudgetStop("additional_budget_not_authorized")
        auth_path = self.folder / "AUTHORIZATION.json"
        old_bytes = auth_path.read_bytes()
        old = json.loads(old_bytes)
        if self._auth_hash(old) != self._authorization_hash or old["max_cny"] != 10:
            raise BudgetStop("extension_requires_original_authorization")
        updated = dict(old, max_cny=15, extension_user_authorization=extension["user_response"])
        self._validate_authorization(updated)
        new_hash = self._auth_hash(updated)
        pending_path = self.folder / "AUTHORIZATION.extension.pending.json"
        audit_path = self.folder / "BUDGET_EXTENSION_ACTIVATED.json"
        if audit_path.exists():
            raise BudgetStop("extension_already_recorded")
        replaced = False
        try:
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                stored = db.execute("SELECT value FROM flags WHERE name='authorization_hash'").fetchone()
                if not stored or stored[0] != self._authorization_hash:
                    raise BudgetStop("authorization_ledger_binding_changed")
                if db.execute("SELECT count(*) FROM calls WHERE status='inflight'").fetchone()[0]:
                    raise BudgetStop("extension_requires_idle_ledger")
                rows, charged = db.execute("SELECT count(*),coalesce(sum(charged),0) FROM calls").fetchone()
                (self.folder / "AUTHORIZATION.before_extension.json").write_bytes(old_bytes)
                pending_path.write_text(json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8")
                pending_path.replace(auth_path)
                replaced = True
                db.execute("UPDATE flags SET value=? WHERE name='authorization_hash'", (new_hash,))
                db.execute("INSERT INTO flags VALUES ('budget_extension',?)", (json.dumps({
                    "from_cny": 10, "to_cny": 15, "calls_preserved": rows,
                    "charged_upper_bound_preserved": charged}),))
        except BaseException:
            if replaced:
                auth_path.write_bytes(old_bytes)
            raise
        audit_path.write_text(json.dumps({"from_cny":10,"to_cny":15,"calls_preserved":rows,
            "charged_upper_bound_preserved":charged,"old_authorization_hash":self._authorization_hash,
            "new_authorization_hash":new_hash,"time_unix":time.time()}, indent=2),encoding="utf-8")
        self.authorization, self._authorization_hash = updated, new_hash
        return self.summary()

    def connect(self):
        return sqlite3.connect(self.path, timeout=30)

    def reserve(self, batch, prompt, max_output):
        auth = self.authorization
        current = json.loads((self.folder / "AUTHORIZATION.json").read_text(encoding="utf-8"))
        if self._auth_hash(current) != self._authorization_hash or self._auth_hash(auth) != self._authorization_hash:
            raise BudgetStop("authorization_changed_for_existing_ledger")
        if type(max_output) is not int or max_output <= 0:
            raise BudgetStop("invalid_output_limit")
        bound = len(prompt.encode("utf-8")) + 4096
        if bound > auth["max_input_bound_per_call"]:
            raise BudgetStop("input_bound_exceeded_no_truncation")
        cost = (bound * 4 + max_output * 16) / 1_000_000
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            stored = db.execute("SELECT value FROM flags WHERE name='authorization_hash'").fetchone()
            if not stored or stored[0] != self._authorization_hash:
                raise BudgetStop("authorization_ledger_binding_changed")
            if db.execute("SELECT value FROM flags WHERE name='halt'").fetchone():
                raise BudgetStop("global_budget_halted")
            count, used = db.execute("SELECT count(*), coalesce(sum(charged),0) FROM calls").fetchone()
            active = db.execute("SELECT count(*) FROM calls WHERE status='inflight'").fetchone()[0]
            if count >= auth["max_calls"] or used + cost > auth["max_cny"]:
                raise BudgetStop("global_call_or_cost_budget_exhausted")
            if active >= auth["max_concurrency"]:
                raise BudgetStop("global_concurrency_exhausted")
            call_id = uuid.uuid4().hex
            db.execute("INSERT INTO calls VALUES (?,?,?,?,?,?,?,?)",
                (call_id, batch, "inflight", cost, cost, bound, max_output, None))
        return call_id, bound

    def finish(self, call_id, usage=None, error=None, *, not_sent=False):
        if not_sent and (not_sent is not True or error != "URLError" or usage is not None):
            raise ValueError("invalid_not_sent_evidence")
        violation = False
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT reserved,input_bound,output_bound,status FROM calls WHERE id=?", (call_id,)).fetchone()
            if row is None:
                raise ValueError("unknown reservation")
            if row[3] != "inflight":
                raise BudgetStop("reservation_already_finished")
            charge, status = row[0], "uncertain" if error else "usage_missing"
            if not_sent:
                charge, status = 0.0, "not_sent"
            if isinstance(usage, dict) and type(usage.get("prompt_tokens")) is int and type(usage.get("completion_tokens")) is int:
                input_n, output_n = usage["prompt_tokens"], usage["completion_tokens"]
                if input_n < 0 or output_n < 0 or input_n > row[1] or output_n > row[2]:
                    db.execute("INSERT OR REPLACE INTO flags VALUES ('halt','token_bound_violation')")
                    status = "invalid_usage"
                    violation = True
                elif not error:
                    charge = (input_n * 2 + output_n * 8) / 1_000_000
                    status = "reported_usage_peak_price_bound"
            db.execute("UPDATE calls SET status=?,charged=?,usage=? WHERE id=?",
                       (status, charge, json.dumps({key: usage[key] for key in ("prompt_tokens", "completion_tokens")
                           if type(usage.get(key)) is int} if isinstance(usage, dict) else None), call_id))
            if error in {"HTTP_401", "HTTP_403", "HTTP_429"}:
                db.execute("INSERT OR REPLACE INTO flags VALUES ('halt',?)", (error,))
        if violation:
            raise BudgetStop("token_bound_violation")

    def summary(self):
        with self.connect() as db:
            rows = db.execute("SELECT status,count(*),sum(charged) FROM calls GROUP BY status").fetchall()
            return {"calls": sum(r[1] for r in rows), "cny_upper_bound": sum(r[2] for r in rows),
                    "states": {r[0]: r[1] for r in rows}, "max_cny": self.authorization["max_cny"],
                    "invoice_cost": None, "pricing_source": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("model_endpoint_redirect_forbidden")


class TrackedHTTPSHandler(urllib.request.HTTPSHandler):
    """Prove a connect-stage failure before any model HTTP request was sent.

    A generic URLError/PermissionError is insufficient: it may occur after
    request transmission. Keep the actual exception identity only in memory.
    """
    def __init__(self, attempt):
        super().__init__()
        self.attempt = attempt

    def https_open(self, req):
        attempt = self.attempt

        class Connection(http.client.HTTPSConnection):
            def connect(self):
                try:
                    super().connect()
                except PermissionError as exc:
                    attempt["preconnect_permission_error"] = exc
                    raise

        return self.do_open(Connection, req, context=self._context)


def _url_error_details(exc):
    reason = exc.reason
    known_types = (PermissionError, ConnectionRefusedError, ConnectionResetError,
                   ConnectionAbortedError, TimeoutError, OSError)
    name = next((kind.__name__ for kind in known_types if isinstance(reason, kind)), "unknown")
    details = {"reason_type": name}
    for key in ("errno", "winerror"):
        value = getattr(reason, key, None)
        if type(value) is int:
            details[key] = value
    return details


class BoundedClient:
    def __init__(self, ledger, batch, out, max_calls, timeout=30, max_tokens=1024, transport=None):
        if type(max_calls) is not int or max_calls <= 0 or type(max_tokens) is not int or max_tokens <= 0:
            raise BudgetStop("invalid_client_limits")
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise BudgetStop("invalid_socket_timeout")
        self.ledger, self.batch, self.out = ledger, batch, Path(out)
        self.max_calls, self.timeout, self.max_tokens = max_calls, timeout, max_tokens
        self.calls, self.attempts = [], 0
        self.model, self.base_url = "deepseek-flash", "https://api.deepseek.com"
        self.transport = transport
        self.out.mkdir(parents=True, exist_ok=True)

    def is_configured(self):
        return bool(self.transport or os.environ.get("DEEPSEEK_API_KEY"))

    def complete(self, prompt, *, purpose=""):
        secret = os.environ.get("DEEPSEEK_API_KEY", "")
        if secret and (secret in prompt or secret in purpose):
            raise BudgetStop("credential_in_model_input")
        if self.attempts >= self.max_calls:
            raise BudgetStop("batch_call_budget_exhausted")
        if not self.is_configured():
            raise BudgetStop("model_key_missing")
        call_id, bound = self.ledger.reserve(self.batch, prompt, self.max_tokens)
        self.attempts += 1
        record = {"call_id": call_id, "purpose": purpose, "model": self.model, "prompt": prompt,
                  "input_bound": bound, "attempt": self.attempts, "transport_retries": 0}
        start = time.monotonic()
        usage, error = None, None
        network_attempt, not_sent = {}, False
        try:
            payload = {"model": self.model, "messages": [{"role": "user", "content": prompt}],
                       "temperature": 0, "max_tokens": self.max_tokens, "thinking": {"type": "disabled"}}
            if self.transport:
                data = self.transport(payload)
            else:
                request = urllib.request.Request(self.base_url + "/chat/completions",
                    data=json.dumps(payload).encode(), method="POST",
                    headers={"Authorization": "Bearer " + os.environ["DEEPSEEK_API_KEY"], "Content-Type": "application/json"})
                with urllib.request.build_opener(NoRedirect, TrackedHTTPSHandler(network_attempt)).open(request, timeout=self.timeout) as response:
                    data = json.load(response)
            usage = data.get("usage")
            message = data["choices"][0]["message"]
            response_text = message.get("content") or message.get("reasoning_content") or ""
            if secret:
                response_text = response_text.replace(secret, "[REDACTED]")
            record.update(response=response_text, usage=usage, returned_model=data.get("model"), ok=True)
            self.calls.append(LLMCall(purpose, prompt, response_text, self.model,
                (usage or {}).get("prompt_tokens", 0), (usage or {}).get("completion_tokens", 0)))
            return response_text
        except Exception as exc:
            error = "HTTP_" + str(exc.code) if isinstance(exc, urllib.error.HTTPError) else type(exc).__name__
            record.update(ok=False, error=error)
            if isinstance(exc, urllib.error.URLError) and not isinstance(exc, urllib.error.HTTPError):
                record["transport_error"] = _url_error_details(exc)
                not_sent = (isinstance(exc.reason, PermissionError)
                            and network_attempt.get("preconnect_permission_error") is exc.reason)
                record["not_sent"] = not_sent
            raise RuntimeError("bounded_model_request_failed:" + error) from None
        finally:
            try:
                self.ledger.finish(call_id, usage, error, not_sent=not_sent)
            except BudgetStop as exc:
                record.update(ok=False, error=str(exc))
                raise
            finally:
                record["seconds"] = round(time.monotonic()-start, 3)
                serialized = json.dumps(record, ensure_ascii=False)
                if secret:
                    serialized = serialized.replace(secret, "[REDACTED]")
                with (self.out / "llm_requests.jsonl").open("a", encoding="utf-8") as fh:
                    fh.write(serialized + "\n")

    def complete_json(self, prompt, *, purpose=""):
        text = self.complete(prompt, purpose=purpose).strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            text = text[4:] if text.startswith("json") else text
        if not text.startswith("{"):
            start, end = text.find("{"), text.rfind("}")
            if start >= 0 and end > start:
                text = text[start:end+1]
        return json.loads(text)

    def dump_calls(self):
        return [c.to_dict() for c in self.calls]
