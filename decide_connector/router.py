"""Router: only:<e> | prefer:<a>,<b>,... (legacy: prefer:<a>,fallback:<b>) | shadow:<primary>,<s1>,..."""
import json
import time
import warnings

from .engines import ContentBlockedError, EngineError


class AllEnginesFailed(EngineError):
    def __init__(self, errors):
        self.errors = dict(errors)  # engine name -> exception
        super().__init__("all engines failed: " + "; ".join(
            "%s: %s: %s" % (n, type(e).__name__, e) for n, e in self.errors.items()))


def parse_policy(p):
    kind, _, rest = p.partition(":")
    parts = [x.split(":")[-1].strip() for x in rest.split(",")]
    if kind == "only" and len(parts) == 1 and parts[0]:
        return kind, parts
    if kind == "prefer" and len(parts) >= 2 and all(parts):
        return kind, parts
    if kind == "shadow" and len(parts) >= 2 and all(parts):
        return kind, parts
    raise ValueError("bad policy %r" % p)


class Router:
    def __init__(self, engines, policy="only:peewee", policies=None, shadow_log=None, fallback_on_block=False):
        self.engines, self.default, self.policies, self.shadow_log = engines, policy, policies or {}, shadow_log
        self.fallback_on_block, self._warned_log = fallback_on_block, False

    def decide(self, state, questions, question_set=None):
        policy = self.policies.get(question_set, self.default)
        kind, names = parse_policy(policy)
        for n in names:
            if n not in self.engines:
                raise ValueError("policy %r names unknown engine %r" % (policy, n))
        if kind == "only":
            res = self.engines[names[0]].decide(state, questions)
            return self._route(res, names[0], "only")
        if kind == "prefer":
            errors = {}
            for n in names:
                try:
                    res = self.engines[n].decide(state, questions)
                except ContentBlockedError as e:
                    if not self.fallback_on_block:
                        raise
                    errors[n] = e
                except EngineError as e:
                    errors[n] = e
                else:
                    if not errors:
                        return self._route(res, n, "primary")
                    first, e = next(iter(errors.items()))
                    return self._route(res, n, "fallback after %s: %s: %s" % (first, type(e).__name__, e))
            raise AllEnginesFailed(errors)
        return self._shadow(state, questions, names, question_set)

    @staticmethod
    def _route(res, name, why):
        res = dict(res)
        res["routed"] = {"engine": name, "reason": why}
        return res

    def _shadow(self, state, questions, names, question_set):
        primary = self.engines[names[0]].decide(state, questions)  # primary failure propagates
        rec = {"ts": time.time(), "question_set": question_set, "primary": primary,
               "secondaries": {}, "errors": {}}
        for n in names[1:]:
            rec["secondaries"][n] = None
            try:
                rec["secondaries"][n] = self.engines[n].decide(state, questions)
            except Exception as e:  # a secondary failure never breaks the primary
                rec["errors"][n] = "%s: %s" % (type(e).__name__, e)
        if self.shadow_log:
            try:
                with open(self.shadow_log, "a") as f:
                    f.write(json.dumps(rec) + "\n")
            except OSError as e:
                if not self._warned_log:
                    self._warned_log = True
                    warnings.warn("shadow log %r not writable: %r" % (self.shadow_log, e), RuntimeWarning, stacklevel=3)
        return self._route(primary, names[0], "shadow primary")
