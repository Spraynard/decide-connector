"""Per-engine question preflight."""
import json

from .engines import CAPABILITIES, Capabilities

TYPES = ("choice", "score", "noul")


class QuestionError(ValueError):
    pass


def validate(questions):
    if not isinstance(questions, dict) or not questions:
        raise QuestionError("questions must be a non-empty dict")
    for qid, q in questions.items():
        if not isinstance(q, dict) or q.get("type") not in TYPES:
            raise QuestionError("%s: type must be one of %s" % (qid, TYPES))
        if not q.get("instructions"):
            raise QuestionError("%s: instructions required" % qid)
        if q["type"] in ("choice", "score") and not q.get("criteria"):
            raise QuestionError("%s: %s needs criteria" % (qid, q["type"]))


def _tok(x):
    return len(x if isinstance(x, str) else json.dumps(x)) // 4


def estimate_tokens(q):
    """Rough head cost (chars/4): instructions + criteria."""
    return _tok(q["instructions"]) + _tok(q.get("criteria") or "")


def budget_warnings(questions, head_budget):
    out = []
    for qid, q in questions.items():
        crit = q.get("criteria")
        if q["type"] == "choice" and isinstance(crit, dict) and crit:
            fixed = _tok(q["instructions"])
            per_opt = max(_tok(v) + _tok(k) for k, v in crit.items())
            if fixed + sum(_tok(v) + _tok(k) for k, v in crit.items()) > head_budget:
                out.append("%s: ~%d tokens vs %d budget; options likely squeezed (~%d tokens/option available, longest option ~%d)"
                           % (qid, estimate_tokens(q), head_budget,
                              max(0, (head_budget - fixed) // len(crit)), per_opt))
        elif estimate_tokens(q) > head_budget:
            out.append("%s: ~%d tokens vs %d budget; likely squeezed" % (qid, estimate_tokens(q), head_budget))
    return out


def _inline(v):
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        return "{" + ", ".join("%s: %s" % (k, _inline(x)) for k, x in v.items()) + "}"
    if isinstance(v, (list, tuple)):
        return "; ".join(_inline(x) for x in v)
    return str(v)


def render_text(x):
    """Deterministic plain-text form of structured instructions/criteria values.
    str unchanged; dict -> 'key: value' lines (nested dicts inline as {k: v, ...});
    list -> items joined with '; '."""
    if isinstance(x, dict):
        return "\n".join("%s: %s" % (k, _inline(v)) for k, v in x.items())
    return _inline(x)


def render_questions(questions):
    out = {}
    for qid, q in questions.items():
        q = dict(q, instructions=render_text(q["instructions"]))
        crit = q.get("criteria")
        if isinstance(crit, dict):
            q["criteria"] = {k: render_text(v) for k, v in crit.items()}
        elif isinstance(crit, list):
            q["criteria"] = [render_text(v) for v in crit]
        out[qid] = q
    return out


def preflight(engine, questions, head_budget=None):
    """Return (questions, warnings) per the engine's capabilities. `engine` is an Engine,
    a Capabilities, or an engine name; an unknown name only validates. `head_budget`
    overrides capabilities.head_budget_tokens."""
    validate(questions)
    caps = engine if isinstance(engine, Capabilities) else (
        getattr(engine, "capabilities", None) if not isinstance(engine, str) else CAPABILITIES.get(engine))
    if caps is None:
        return questions, []
    warns = []
    for qid, q in questions.items():
        if q["type"] not in caps.types:
            raise QuestionError("%s: engine does not support type %r" % (qid, q["type"]))
        crit = q.get("criteria")
        if caps.max_options is not None and q["type"] in ("choice", "score") and len(crit) > caps.max_options:
            raise QuestionError("%s: %d options exceeds engine max_options %d" % (qid, len(crit), caps.max_options))
    budget = head_budget or caps.head_budget_tokens
    if budget:
        warns = budget_warnings(questions, budget)
    if not caps.structured_instructions:
        questions = render_questions(questions)
    return questions, warns


def long_state_pipe(engine_name, state, questions, mode="truncate"):
    """HOOK (not implemented): later long-state handling, mode 'truncate' or 'windows'."""
    raise NotImplementedError("long-state pipe is step 2")
