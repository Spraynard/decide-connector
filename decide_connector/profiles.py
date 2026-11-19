"""Threshold profiles keyed by (engine, version), loaded from JSON."""
import json
import warnings


class ProfileError(KeyError):
    pass


class Profiles:
    def __init__(self, data):
        self._data, self._warned = data, set()

    @classmethod
    def load(cls, path):
        with open(path) as f:
            return cls(json.load(f))

    def _profile(self, engine, version):
        try:
            return self._data[engine][version]
        except KeyError:
            raise ProfileError("no profile for engine=%r version=%r (have: %s)" % (
                engine, version, {e: list(v) for e, v in self._data.items()})) from None

    def get(self, engine, version, key):
        p = self._profile(engine, version)
        if key not in p.get("thresholds", {}):
            raise ProfileError("profile %s/%s has no threshold %r (have: %s)" % (
                engine, version, key, sorted(p.get("thresholds", {}))))
        if p.get("tuned") is False and (engine, version) not in self._warned:
            self._warned.add((engine, version))
            warnings.warn("profile %s/%s is untuned; thresholds are placeholders" % (engine, version),
                          UserWarning, stacklevel=2)
        return p["thresholds"][key]
