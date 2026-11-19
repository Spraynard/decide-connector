"""Engines answering typed questions. Every engine returns the response dict plus
engine, engine_version, latency_ms and warnings."""
import json
import math
import os
import re
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass


class EngineError(RuntimeError):
    pass


class CreditError(EngineError):
    pass


class RateLimitError(EngineError):
    pass


class ContentBlockedError(EngineError):
    """The engine refused the content (safety / blocklist). Retrying elsewhere is a policy decision."""


@dataclass(frozen=True)
class Capabilities:
    types: frozenset = frozenset({"choice", "score", "noul"})
    max_state_tokens: int = None
    head_budget_tokens: int = None      # None = no per-question budget
    max_options: int = None
    calibrated: bool = False
    batch: bool = False
    cost_per_m_input: float = None
    structured_instructions: bool = True  # accepts dict instructions/criteria natively


ALL_TYPES = frozenset({"choice", "score", "noul"})
JEV_CAPS = Capabilities(ALL_TYPES, 32_000, None, None, True, False, 0.042, True)
PEEWEE_CAPS = Capabilities(ALL_TYPES, 700, 256, None, False, True, 0.0, True)
GEMINI_CAPS = Capabilities(ALL_TYPES, 262_144, None, 255, False, False, None, False)
CAPABILITIES = {"jev": JEV_CAPS, "peewee": PEEWEE_CAPS, "gemini": GEMINI_CAPS}


def usage_warnings(usage):
    """Pull truncation / squeeze notices out of a (Peewee) usage dict."""
    out, usage = [], usage or {}
    if usage.get("truncated"):
        out.append("state truncated (%s tokens dropped)" % usage.get("state_tokens_dropped", "?"))

    def walk(node, path):
        if isinstance(node, dict):
            if node.get("options_squeezed"):
                out.append("options squeezed: %s" % (path or "?"))
            for k, v in node.items():
                walk(v, k)
        elif isinstance(node, list):
            for v in node:
                walk(v, path)
    walk({k: v for k, v in usage.items() if k not in ("truncated", "state_tokens_dropped")}, "")
    return out


class Engine:
    name = "engine"
    version = ""
    capabilities = Capabilities()
    model = None

    def to_engine(self, state, questions, model=None):
        """Canonical -> engine request body (identity for Jev/Peewee)."""
        body = {"state": state, "questions": questions}
        if model:
            body["model"] = model
        return body

    def from_engine(self, resp):
        """Engine response -> canonical response (identity for Jev/Peewee)."""
        return resp

    def _raw(self, state, questions):
        raise NotImplementedError

    def _normalise(self, resp, t0):
        res = dict(resp)
        res.update(engine=self.name, engine_version=self.version,
                   latency_ms=round((time.monotonic() - t0) * 1000, 1),
                   warnings=list(resp.get("warnings") or []) + usage_warnings(resp.get("usage")))
        return res

    def decide(self, state, questions):
        t0 = time.monotonic()
        return self._normalise(self._raw(state, questions), t0)

    def decide_many(self, requests):
        return [self.decide(r["state"], r["questions"]) for r in requests]


class _HTTPEngine(Engine):
    def __init__(self, base_url, path="/v1/decide", api_key=None, model=None, timeout=30.0, opener=None):
        self.base_url, self.path = base_url.rstrip("/"), path
        self.api_key, self.model, self.timeout = api_key, model, timeout
        self._open = opener or urllib.request.urlopen

    def _headers(self):
        return {"Authorization": "Bearer " + self.api_key} if self.api_key else {}

    def _map_http_error(self, e, msg):
        if e.code == 402:
            return CreditError(msg)
        if e.code == 429:
            return RateLimitError(msg)
        return EngineError(msg)

    def _post(self, path, body):
        req = urllib.request.Request(self.base_url + path, data=json.dumps(body).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        for k, v in self._headers().items():
            req.add_header(k, v)
        try:
            with self._open(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            msg = "%s HTTP %d: %s" % (self.name, e.code, e.reason)
            raise self._map_http_error(e, msg) from e
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError, ValueError) as e:
            raise EngineError("%s request failed: %r" % (self.name, e)) from e

    def _raw(self, state, questions):
        return self.from_engine(self._post(self.path, self.to_engine(state, questions, self.model)))


class JevEngine(_HTTPEngine):
    name = "jev"
    capabilities = JEV_CAPS

    def __init__(self, base_url=None, path=None, api_key=None, model="jev-1.14.0", **kw):
        super().__init__(base_url or os.environ.get("JEV_BASE_URL", "https://api.typesafe.ai"),
                         path or os.environ.get("JEV_PATH", "/v1/decide"),
                         api_key or os.environ.get("TYPESAFE_API_KEY"), model, **kw)
        self.version = model


class PeeweeHTTPEngine(_HTTPEngine):
    name = "peewee"
    capabilities = PEEWEE_CAPS

    def __init__(self, base_url="http://localhost:8000", version="mix-v1", truncate=None, **kw):
        super().__init__(base_url, **kw)
        self.version, self.truncate = version, truncate

    def to_engine(self, state, questions, model=None):
        body = super().to_engine(state, questions, model)
        if self.truncate:
            body["truncate"] = self.truncate
        return body

    def decide_many(self, requests):
        t0 = time.monotonic()
        res = self._post("/v1/decide/batch", {"requests": list(requests)})["results"]
        return [self._normalise(r, t0) for r in res]


class PeeweeLocalEngine(Engine):
    """In-process Peewee; the model loads on first use and is kept."""
    name = "peewee"
    capabilities = PEEWEE_CAPS

    def __init__(self, model_id="roadius/peewee-mix-v1", version="mix-v1", calibration=None, truncate=None):
        self.model_id, self.version = model_id, version
        self.calibration, self.truncate, self._agent = calibration, truncate, None

    def _load(self):
        if self._agent is None:
            import peewee_decide  # lazy: only present in the peewee venv
            self._agent = peewee_decide.load(self.model_id, calibration=self.calibration, truncate=self.truncate)
        return self._agent

    def _raw(self, state, questions):
        try:
            return self._load().system_one(state, questions)
        except ImportError as e:
            raise EngineError("peewee_decide not importable: %s" % e) from e
        except Exception as e:
            raise EngineError("peewee local failure: %r" % e) from e


_KEY_RE = re.compile(r"[A-Za-z0-9_-]+")
_BLOCK_CODES = {"safety", "prohibited_content", "blocklist", "image_safety", "content_blocked"}


def _entropy(probs):
    """Normalised Shannon entropy in [0, 1]."""
    ps = list(probs.values()) if isinstance(probs, dict) else list(probs)
    if len(ps) < 2:
        return 0.0
    return max(0.0, -sum(p * math.log(p) for p in ps if p > 0) / math.log(len(ps)))


class GeminiEngine(_HTTPEngine):
    """Gemini Decisions API (v1beta). Text-only input; instructions/criteria are plain strings."""
    name = "gemini"
    capabilities = GEMINI_CAPS

    def __init__(self, base_url=None, api_key=None, model="gemini-decision-1", **kw):
        super().__init__(base_url or os.environ.get("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com"),
                         "/v1beta/decisions", api_key or os.environ.get("GEMINI_API_KEY"), model, **kw)
        self.version = model

    def _headers(self):
        return {"x-goog-api-key": self.api_key} if self.api_key else {}

    def to_engine(self, state, questions, model=None):
        from .adapt import render_text
        if not isinstance(questions, dict) or not 1 <= len(questions) <= 1000:
            raise ValueError("gemini: 1-1000 questions per request")
        qs = {}
        for qid, q in questions.items():
            if not _KEY_RE.fullmatch(str(qid)) or len(qid) > 64:
                raise ValueError("gemini: question key %r must match [A-Za-z0-9_-]+ and be <= 64 chars" % qid)
            out = {"type": q["type"], "instructions": render_text(q["instructions"])}
            crit = q.get("criteria")
            if q["type"] == "noul":
                if crit:
                    m = {"true": "true_description", "false": "false_description"}
                    out["criteria"] = {m[k]: render_text(v) for k, v in crit.items() if k in m}
            elif q["type"] == "choice":
                if not isinstance(crit, dict) or not 2 <= len(crit) <= 255:
                    raise ValueError("gemini: %s: choice needs 2-255 options" % qid)
                for k in crit:
                    if not 1 <= len(str(k)) <= 128:
                        raise ValueError("gemini: %s: option key %r must be 1-128 chars" % (qid, k))
                out["criteria"] = {k: render_text(v) for k, v in crit.items()}
            else:
                if not isinstance(crit, list) or not 2 <= len(crit) <= 255:
                    raise ValueError("gemini: %s: score needs 2-255 levels" % qid)
                lv = [render_text(x) for x in crit]
                if not all(lv):
                    raise ValueError("gemini: %s: score levels must be non-empty strings" % qid)
                out["criteria"] = lv
            qs[qid] = out
        text = state if isinstance(state, str) else json.dumps(state, sort_keys=False, ensure_ascii=False)
        return {"model": model or self.model, "input": text, "questions": qs}

    def from_engine(self, resp):
        err = resp.get("error")
        if isinstance(err, dict) and err.get("status") == "RESOURCE_EXHAUSTED":
            raise RateLimitError("gemini: %s" % err.get("message", "RESOURCE_EXHAUSTED"))
        res, warns, answers = dict(resp), [], {}
        for qid, a in (resp.get("answers") or {}).items():
            a = dict(a)
            if "noul" in a:
                p = a["noul"]
                a["entropy"] = _entropy([1 - p, p])
                if min(p, 1 - p) <= 0.01 + 1e-9:
                    warns.append("%s: noul at clamp bound (%s); probabilities are clamped to 0.01-0.99" % (qid, p))
            elif "probabilities" in a:
                a["entropy"] = _entropy(a["probabilities"])
            if "entropy" in a:
                a["entropy_source"] = "computed"
            answers[qid] = a
        res["answers"], res["warnings"] = answers, warns
        return res

    def _map_http_error(self, e, msg):
        try:
            err = json.loads(e.read().decode()).get("error") or {}
        except Exception:
            err = {}
        code, status, text = err.get("code"), err.get("status"), err.get("message") or msg
        # The docs page lists no quota/rate-limit codes; 429 / RESOURCE_EXHAUSTED is assumed standard Google behaviour.
        if e.code == 429 or status == "RESOURCE_EXHAUSTED":
            return RateLimitError("gemini: %s" % text)
        if e.code == 402:
            return CreditError("gemini: %s" % text)
        if e.code == 400 and (code in _BLOCK_CODES or status in _BLOCK_CODES):
            return ContentBlockedError("gemini: blocked (%s): %s" % (code or status, text))
        if e.code == 400:
            return EngineError("gemini HTTP 400: %s" % text)
        return EngineError(msg)
