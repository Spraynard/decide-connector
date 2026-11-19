"""Engine registry: register(name, factory) / create(name, **kw); plus entry-point discovery
(group 'decide_connector.engines'; each entry point loads to a factory)."""
from importlib import metadata

from .engines import GeminiEngine, JevEngine, PeeweeHTTPEngine, PeeweeLocalEngine

GROUP = "decide_connector.engines"
_factories = {"jev": JevEngine, "peewee-http": PeeweeHTTPEngine,
              "peewee-local": PeeweeLocalEngine, "gemini": GeminiEngine}
_discovered = False


def register(name, factory):
    _factories[name] = factory


def _entry_points():
    try:
        return list(metadata.entry_points(group=GROUP))
    except Exception:
        return []


def _discover():
    global _discovered
    if _discovered:
        return
    _discovered = True
    for ep in _entry_points():
        _factories.setdefault(ep.name, ep.load())  # built-ins and explicit register() win


def available():
    _discover()
    return sorted(_factories)


def create(name, **kw):
    if name not in _factories:
        _discover()
    if name not in _factories:
        raise KeyError("unknown engine %r (have: %s)" % (name, sorted(_factories)))
    return _factories[name](**kw)
