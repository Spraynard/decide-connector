import io
import json
import socket
import urllib.error
import warnings

import pytest

from decide_connector import (CreditError, EngineError, JevEngine, PeeweeHTTPEngine, Profiles,
                              RateLimitError, Router)
from decide_connector.adapt import QuestionError, preflight, validate
from decide_connector.compare import compare, format_table
from decide_connector.engines import Engine
from decide_connector.profiles import ProfileError

RESP = {"model": "m", "answers": {"q": {"noul": 0.9}}, "usage": {}}


class Resp:
    def __init__(self, d): self.b = json.dumps(d).encode()
    def read(self): return self.b
    def __enter__(self): return self
    def __exit__(self, *a): pass


def opener_for(d=None, exc=None):
    seen = []
    def op(req, timeout=None):
        seen.append(req)
        if exc:
            raise exc
        return Resp(d)
    op.seen = seen
    return op


def http_err(code):
    return urllib.error.HTTPError("u", code, "boom", {}, io.BytesIO(b"{}"))


@pytest.mark.parametrize("exc,cls", [
    (http_err(402), CreditError), (http_err(429), RateLimitError), (http_err(500), EngineError),
    (socket.timeout("t"), EngineError), (urllib.error.URLError("down"), EngineError)])
def test_error_mapping(exc, cls):
    with pytest.raises(cls) as ei:
        JevEngine(api_key="k", opener=opener_for(exc=exc)).decide("s", {})
    assert type(ei.value) is cls and isinstance(ei.value, EngineError)


def test_normalise_and_warnings_and_request():
    resp = dict(RESP, usage={"truncated": True, "state_tokens_dropped": 40,
                             "questions": {"dept": {"options_squeezed": True}}})
    op = opener_for(resp)
    e = JevEngine(api_key="k", opener=op)
    r = e.decide("s", {"q": {}})
    assert r["engine"] == "jev" and r["engine_version"] == "jev-1.14.0" and r["latency_ms"] >= 0
    assert r["answers"] == resp["answers"]
    assert any("truncated" in w and "40" in w for w in r["warnings"])
    assert any("squeezed" in w and "dept" in w for w in r["warnings"])
    req = op.seen[0]
    assert req.full_url == "https://api.typesafe.ai/v1/decide"
    assert req.get_header("Authorization") == "Bearer k"
    assert json.loads(req.data)["model"] == "jev-1.14.0"


def test_peewee_http_batch():
    op = opener_for({"results": [RESP, RESP]})
    out = PeeweeHTTPEngine(opener=op).decide_many([{"state": 1, "questions": {}}] * 2)
    assert len(out) == 2 and out[0]["engine"] == "peewee" and op.seen[0].full_url.endswith("/v1/decide/batch")


Q = {"q": {"type": "noul", "instructions": "x"}}


def test_validate():
    validate(Q)
    for bad in [{}, {"q": {"type": "bogus", "instructions": "x"}}, {"q": {"type": "noul"}},
                {"q": {"type": "choice", "instructions": "x"}}, {"q": {"type": "score", "instructions": "x"}}]:
        with pytest.raises(QuestionError):
            validate(bad)


def test_squeeze_warning_and_jev_passthrough():
    big = {"q": {"type": "choice", "instructions": "pick",
                 "criteria": {"o%d" % i: "d" * 200 for i in range(10)}}}
    assert preflight("peewee", big)[1] and "q" in preflight("peewee", big)[1][0]
    assert preflight("peewee", big, head_budget=10000)[1] == []
    qs, w = preflight("jev", big)
    assert qs is big and w == []


class Fake(Engine):
    def __init__(self, name, fail=None):
        self.name, self.version, self.fail, self.calls = name, "v", fail, 0

    def _raw(self, state, questions):
        self.calls += 1
        if self.fail:
            raise self.fail
        return {"answers": {"q": {"choice": self.name, "probabilities": {"a": 0.6, "b": 0.4}}}}


def test_only_and_prefer_fallback():
    j, p = Fake("jev", CreditError("402")), Fake("peewee")
    r = Router({"jev": j, "peewee": p}, "only:peewee").decide("s", Q)
    assert r["engine"] == "peewee" and j.calls == 0
    r = Router({"jev": j, "peewee": p}, "prefer:jev,fallback:peewee").decide("s", Q)
    assert r["engine"] == "peewee" and "CreditError" in r["routed"]["reason"]
    r = Router({"jev": Fake("jev"), "peewee": p}, "prefer:jev,fallback:peewee").decide("s", Q)
    assert r["engine"] == "jev" and r["routed"]["reason"] == "primary"


def test_per_set_policy_and_bad_policy():
    rt = Router({"a": Fake("a"), "b": Fake("b")}, "only:a", policies={"s2": "only:b"})
    assert rt.decide("s", Q, "s2")["engine"] == "b"
    with pytest.raises(ValueError):
        Router({"a": Fake("a")}, "nonsense").decide("s", Q)


def test_shadow(tmp_path):
    log = tmp_path / "l.jsonl"
    rt = Router({"a": Fake("a"), "b": Fake("b")}, "shadow:a,b", shadow_log=str(log))
    assert rt.decide("s", Q)["engine"] == "a"
    rt = Router({"a": Fake("a"), "b": Fake("b", RateLimitError("429"))}, "shadow:a,b", shadow_log=str(log))
    assert rt.decide("s", Q)["engine"] == "a"
    recs = [json.loads(x) for x in log.read_text().splitlines()]
    assert recs[0]["secondaries"]["b"]["engine"] == "b" and recs[1]["secondaries"]["b"] is None
    assert "RateLimitError" in recs[1]["errors"]["b"]


def test_profiles():
    ps = Profiles({"jev": {"v1": {"tuned": True, "thresholds": {"X": 0.7}}},
                   "peewee": {"m": {"tuned": False, "thresholds": {"X": 0.5}}}})
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert ps.get("jev", "v1", "X") == 0.7
    with pytest.warns(UserWarning, match="untuned"):
        assert ps.get("peewee", "m", "X") == 0.5
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        ps.get("peewee", "m", "X")  # only once
    with pytest.raises(ProfileError, match="Y"):
        ps.get("jev", "v1", "Y")
    with pytest.raises(ProfileError):
        ps.get("jev", "v9", "X")


def test_example_profiles_load():
    ps = Profiles.load("profiles.example.json")
    assert ps.get("jev", "jev-1.14.0", "P_TOP_MIN") == 0.70


def test_compare(tmp_path):
    def rec(c1, c2, n1, n2):
        mk = lambda c, n, p: {"answers": {"dept": {"choice": c, "probabilities": {"a": p, "b": 1 - p}},
                                          "bill": {"noul": n}}}
        return {"primary": mk(c1, n1, 0.8), "secondary": mk(c2, n2, 0.6)}
    p = tmp_path / "l.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in [rec("a", "a", 0.9, 0.7), rec("a", "b", 0.9, 0.2),
                                                   {"primary": rec("a", "a", 0, 0)["primary"], "secondary": None}]))
    s = compare(str(p))
    assert s["dept"]["n"] == 2 and sum(s["dept"]["top1"]) == 1
    assert s["bill"]["noul"] == [True, False]
    assert s["bill"]["mad"] == pytest.approx([0.2, 0.7])
    assert s["dept"]["mad"] == pytest.approx([0.2, 0.2])
    assert "dept" in format_table(s)


# ---- capabilities, gemini, registry, router chains ----
from decide_connector import (AllEnginesFailed, ContentBlockedError, GeminiEngine, create, register)
from decide_connector import registry
from decide_connector.adapt import render_text
from decide_connector.engines import GEMINI_CAPS, JEV_CAPS, PEEWEE_CAPS

GQ = {"dept": {"type": "choice", "instructions": {"task": "route", "rules": ["a", "b"]},
               "criteria": {"x": {"means": "ex", "not": ["y"]}, "z": ""}},
      "bill": {"type": "noul", "instructions": "billing?", "criteria": {"true": "yes", "false": "no"}},
      "sev": {"type": "score", "instructions": "how bad", "criteria": ["low", "high"]}}


def gem(d=None, exc=None):
    return GeminiEngine(base_url="http://g/", api_key="K", opener=opener_for(d, exc))


def test_capabilities_values():
    assert JEV_CAPS.cost_per_m_input == 0.042 and JEV_CAPS.calibrated and JEV_CAPS.head_budget_tokens is None
    assert PEEWEE_CAPS.head_budget_tokens == 256 and PEEWEE_CAPS.batch and PEEWEE_CAPS.max_state_tokens == 700
    assert GEMINI_CAPS.max_options == 255 and not GEMINI_CAPS.structured_instructions


def test_render_text():
    assert render_text("s") == "s"
    assert render_text({"a": "x", "b": ["p", "q"], "c": {"k": "v", "l": ["m", "n"]}}) == \
        "a: x\nb: p; q\nc: {k: v, l: m; n}"
    assert render_text(["a", "b"]) == "a; b"


def test_gemini_to_engine():
    e = gem()
    b = e.to_engine({"k": "é"}, GQ, "gemini-decision-1")
    assert b["input"] == '{"k": "é"}' and b["model"] == "gemini-decision-1"
    q = b["questions"]
    assert q["bill"]["criteria"] == {"true_description": "yes", "false_description": "no"}
    assert q["dept"]["instructions"] == "task: route\nrules: a; b"
    assert q["dept"]["criteria"] == {"x": "means: ex\nnot: y", "z": ""}
    assert q["sev"]["criteria"] == ["low", "high"]
    assert e.to_engine("plain", {"b": GQ["bill"]}, "m")["input"] == "plain"
    assert "criteria" not in e.to_engine("s", {"b": {"type": "noul", "instructions": "i"}}, "m")["questions"]["b"]


@pytest.mark.parametrize("qs,rule", [
    ({"bad key": GQ["bill"]}, "question key"),
    ({"k" * 65: GQ["bill"]}, "question key"),
    ({}, "1-1000"),
    ({"c": {"type": "choice", "instructions": "i", "criteria": {"a": "x"}}}, "2-255"),
    ({"c": {"type": "choice", "instructions": "i", "criteria": {"a": "x", "b" * 129: "y"}}}, "1-128"),
    ({"c": {"type": "score", "instructions": "i", "criteria": ["a", ""]}}, "non-empty"),
    ({"c": {"type": "score", "instructions": "i", "criteria": ["a"] * 256}}, "2-255"),
])
def test_gemini_validation(qs, rule):
    with pytest.raises(ValueError, match=rule):
        gem().to_engine("s", qs, "m")


def test_gemini_from_engine_and_decide():
    d = {"answers": {"bill": {"type": "noul", "noul": 0.99, "confidence": 0.8},
                     "dept": {"type": "choice", "choice": "x", "probabilities": {"x": 0.5, "z": 0.5}, "confidence": 0.5},
                     "mid": {"type": "noul", "noul": 0.5, "confidence": 0.5}},
         "usage": {"total_input_tokens": 7}}
    e = gem(d)
    r = e.decide("s", GQ)
    a = r["answers"]
    assert a["dept"]["entropy"] == pytest.approx(1.0) and a["dept"]["entropy_source"] == "computed"
    assert a["mid"]["entropy"] == pytest.approx(1.0)
    assert a["bill"]["entropy"] < 0.1
    assert len(r["warnings"]) == 1 and "bill" in r["warnings"][0] and "clamp" in r["warnings"][0]
    assert r["usage"] == {"total_input_tokens": 7} and r["engine"] == "gemini"
    req = e._open.seen[0]
    assert req.full_url == "http://g/v1beta/decisions" and req.get_header("X-goog-api-key") == "K"
    assert json.loads(req.data)["questions"]["bill"]["criteria"]["true_description"] == "yes"


def gerr(code, body):
    return urllib.error.HTTPError("u", code, "boom", {}, io.BytesIO(json.dumps(body).encode()))


@pytest.mark.parametrize("code,body,cls", [
    (400, {"error": {"code": "safety", "message": "no"}}, ContentBlockedError),
    (400, {"error": {"code": 400, "status": "content_blocked", "message": "no"}}, ContentBlockedError),
    (400, {"error": {"code": "invalid", "message": "bad q"}}, EngineError),
    (429, {}, RateLimitError),
    (400, {"error": {"status": "RESOURCE_EXHAUSTED", "message": "q"}}, RateLimitError),
    (402, {}, CreditError),
])
def test_gemini_errors(code, body, cls):
    with pytest.raises(cls) as ei:
        gem(exc=gerr(code, body)).decide("s", GQ)
    assert type(ei.value) is cls
    if cls is EngineError:
        assert "bad q" in str(ei.value)


def test_gemini_200_resource_exhausted():
    with pytest.raises(RateLimitError):
        gem({"error": {"status": "RESOURCE_EXHAUSTED"}}).decide("s", GQ)


def test_registry(monkeypatch):
    assert isinstance(create("gemini", api_key="k"), GeminiEngine)
    assert create("jev", api_key="k").name == "jev"
    assert create("peewee-http").name == "peewee" and create("peewee-local").name == "peewee"

    class EP:
        name = "fake-ep"
        def load(self): return lambda **kw: Fake("fake")
    monkeypatch.setattr(registry, "_entry_points", lambda: [EP()])
    monkeypatch.setattr(registry, "_discovered", False)
    monkeypatch.setattr(registry, "_factories", dict(registry._factories))
    assert create("fake-ep").name == "fake"
    register("mine", lambda **kw: Fake("mine"))
    assert create("mine").name == "mine"
    with pytest.raises(KeyError):
        create("nope")


def test_registry_no_entry_points(monkeypatch):
    monkeypatch.setattr(registry, "_entry_points", lambda: [])
    monkeypatch.setattr(registry, "_discovered", False)
    assert "gemini" in registry.available()


def test_capability_preflight():
    big = {"q": {"type": "choice", "instructions": "pick", "criteria": {"o%d" % i: "d" * 200 for i in range(300)}}}
    with pytest.raises(QuestionError, match="max_options"):
        preflight("gemini", big)
    assert preflight("peewee", big)[1]  # squeeze warning from capabilities
    for name in ("jev", "gemini"):
        ok = {"q": {"type": "choice", "instructions": "pick", "criteria": {"o%d" % i: "d" * 200 for i in range(10)}}}
        assert preflight(name, ok)[1] == []
    qs, _ = preflight("gemini", GQ)
    assert qs["dept"]["instructions"] == "task: route\nrules: a; b"
    assert preflight("jev", GQ)[0] is GQ and preflight(GeminiEngine(api_key="k"), GQ)[0] is not GQ
    from decide_connector import Capabilities
    with pytest.raises(QuestionError, match="type"):
        preflight(Capabilities(types=frozenset({"choice"})), GQ)


def test_prefer_chain_and_all_failed():
    a, b, c = Fake("a", RateLimitError("rl")), Fake("b", CreditError("cr")), Fake("c")
    r = Router({"a": a, "b": b, "c": c}, "prefer:a,b,c").decide("s", Q)
    assert r["engine"] == "c" and "RateLimitError" in r["routed"]["reason"] and b.calls == 1
    b2 = Fake("b", EngineError("boom"))
    with pytest.raises(AllEnginesFailed) as ei:
        Router({"a": a, "b": b2}, "prefer:a,fallback:b").decide("s", Q)
    assert set(ei.value.errors) == {"a", "b"} and isinstance(ei.value, EngineError)
    assert "rl" in str(ei.value) and "boom" in str(ei.value)


def test_content_blocked_no_fallback_by_default():
    a, b = Fake("a", ContentBlockedError("blocked")), Fake("b")
    with pytest.raises(ContentBlockedError):
        Router({"a": a, "b": b}, "prefer:a,b").decide("s", Q)
    assert b.calls == 0
    r = Router({"a": a, "b": b}, "prefer:a,b", fallback_on_block=True).decide("s", Q)
    assert r["engine"] == "b" and "ContentBlockedError" in r["routed"]["reason"]


def test_shadow_two_secondaries_and_log_warning(tmp_path):
    log = tmp_path / "l.jsonl"
    es = {"p": Fake("p"), "s1": Fake("s1"), "s2": Fake("s2", EngineError("x"))}
    assert Router(es, "shadow:p,s1,s2", shadow_log=str(log)).decide("s", Q)["engine"] == "p"
    rec = json.loads(log.read_text())
    assert rec["secondaries"]["s1"]["engine"] == "s1" and rec["secondaries"]["s2"] is None
    assert "x" in rec["errors"]["s2"] and set(rec) >= {"ts", "question_set", "primary"}
    rt = Router(es, "shadow:p,s1", shadow_log=str(tmp_path / "nodir" / "l.jsonl"))
    with pytest.warns(RuntimeWarning, match="not writable"):
        rt.decide("s", Q)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        rt.decide("s", Q)  # warns once only


def test_compare_new_and_old_formats(tmp_path):
    mk = lambda c, n: {"answers": {"dept": {"choice": c, "probabilities": {"a": 0.5, "b": 0.5}}, "bill": {"noul": n}}}
    recs = [{"primary": mk("a", 0.9), "secondaries": {"s1": mk("a", 0.8), "s2": mk("b", 0.1), "s3": None}},
            {"primary": mk("a", 0.9), "secondary": mk("b", 0.9)}]
    p = tmp_path / "l.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in recs))
    s = compare(str(p))
    assert s["dept [s1]"]["top1"] == [True] and s["dept [s2]"]["top1"] == [False]
    assert s["bill [s2]"]["noul"] == [False] and s["dept"]["top1"] == [False]
    assert "s3" not in format_table(s)
