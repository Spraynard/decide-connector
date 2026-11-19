# decide-connector

Send typed questions (choice / score / noul) to Jev (hosted) or Peewee (local) behind one interface.
Stdlib only; `peewee_decide` is imported lazily by `PeeweeLocalEngine`. Step 1 of `handler/DECIDE-CONNECTOR.md`.

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
