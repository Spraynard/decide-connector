# decide-connector

Send typed questions (choice / score / noul) to Jev (hosted) or Peewee (local) behind one interface.
Stdlib only; `peewee_decide` is imported lazily by `PeeweeLocalEngine`. Step 1 of `handler/DECIDE-CONNECTOR.md`.

## Usage
```python
from decide_connector import JevEngine, PeeweeHTTPEngine, Profiles, Router, AllEnginesFailed
from decide_connector.adapt import preflight

state = {"subject": "Billed twice", "body": "I was charged twice for my March invoice, please refund one."}
questions = {
    "is_billing": {"type": "noul", "instructions": "Is this ticket about billing?"},
    "dept": {"type": "choice", "instructions": "Which department should handle this ticket?",
             "criteria": {"billing": "payments, invoices, refunds",
                          "support": "how-to and product help",
                          "sales": "pricing and new purchases"}},
    "urgency": {"type": "score", "instructions": "How urgent is this ticket?",
                "criteria": ["not urgent", "very urgent"]},
}

# Validate and adapt the questions for the engine you expect to answer them.
questions, warnings = preflight("peewee", questions)

# Peewee first; fall back to Jev on any EngineError (outage, rate limit, credits).
router = Router({"peewee": PeeweeHTTPEngine("http://localhost:8000"), "jev": JevEngine()},
                policy="prefer:peewee,jev")
try:
    res = router.decide(state, questions)
except AllEnginesFailed as e:
    raise SystemExit("no engine answered: %s" % e.errors)

answers = res["answers"]
print(res["routed"])               # {"engine": "peewee", "reason": "primary"}
print(answers["dept"]["choice"], answers["dept"]["probabilities"])
print(answers["is_billing"]["noul"], res["latency_ms"], res["warnings"])

# Engine-specific thresholds (warns once if that profile is untuned).
p_top_min = Profiles.load("profiles.example.json").get(res["engine"], res["engine_version"], "P_TOP_MIN")
if max(answers["dept"]["probabilities"].values()) < p_top_min:
    print("low confidence: send to a human")
```
A single engine works the same without a router: `PeeweeHTTPEngine().decide(state, questions)`.

## Config
- `JevEngine(base_url, path, api_key, model)`: defaults `https://api.typesafe.ai`, `/v1/decide`, `$TYPESAFE_API_KEY`
  (env `JEV_BASE_URL` / `JEV_PATH` also read), model `jev-1.14.0`. HTTP 402 -> `CreditError`, 429 -> `RateLimitError`, other failures -> `EngineError`.
- `PeeweeHTTPEngine(base_url)` or `PeeweeLocalEngine(model_id)` (in-process; ~70 s first load).
- Every result = response dict + `engine`, `engine_version`, `latency_ms`, `warnings`.
- `profiles.example.json`: thresholds per engine/version; `Profiles.load(path).get("jev","jev-1.14.0","P_TOP_MIN")`. Untuned profiles warn once.
- `adapt.preflight(engine, questions)` validates and warns about likely Peewee option squeezing. Long-state pipe is a stub (`long_state_pipe`).

## Engines (canonical schema = Jev/Peewee shape; each engine has `to_engine` / `from_engine`)
| engine | registry name | state tokens | head budget | max options | calibrated | batch | $/M in | dict instructions |
|---|---|---|---|---|---|---|---|---|
| Jev | `jev` | 32k | none | none | yes | no | 0.042 | native |
| Peewee | `peewee-http`, `peewee-local` | 700 | 256 | none | per-dataset only | yes | 0 | native |
| Gemini Decisions | `gemini` | 262k | none | 255 | unknown | no | n/a | rendered to text |

Gemini: `GeminiEngine(base_url, api_key, model="gemini-decision-1")` (`$GEMINI_API_KEY`, `$GEMINI_BASE_URL`); validates key/option limits,
computes `entropy` (marked `entropy_source: "computed"`), warns on clamped noul. 400 safety codes -> `ContentBlockedError`; 429 / `RESOURCE_EXHAUSTED` -> `RateLimitError`.
Registry: `registry.create(name, **kw)`, `register(name, factory)`; third-party engines via entry-point group `decide_connector.engines`.
`adapt.preflight(engine_or_name, questions)` reads capabilities: type/option-count errors, head-budget squeeze warning (only if set), dict -> text rendering when not structured.

## Policies (`Router(engines, policy, policies={set: policy}, shadow_log=path, fallback_on_block=False)`)
- `only:<e>`
- `prefer:<a>,<b>,<c>...` (legacy `prefer:<a>,fallback:<b>`): tries in order on any `EngineError`; `routed.reason` names the fallback.
  All failing raises `AllEnginesFailed` (`.errors` = every engine's exception).
- `ContentBlockedError` is NOT retried on the next engine unless `fallback_on_block=True` (a blocked prompt is a content decision, not an outage).
- `shadow:<primary>,<s1>,<s2>...`: returns primary; log record `{ts, question_set, primary, secondaries:{name: resp|null}, errors:{name: str}}`.
  Secondary failures are logged, never raised; a log write failure warns once per Router.

Compare a shadow log: `python -m decide_connector.compare log.jsonl [noul_threshold]`

## Tests
`python3 -m pytest -q` (fakes only). Live check: `~/.venvs/peewee/bin/python -I scripts/smoke_peewee_local.py`
