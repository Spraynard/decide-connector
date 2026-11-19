"""Report agreement from a shadow JSONL log: python -m decide_connector.compare <log.jsonl> [threshold]"""
import json
import sys
from collections import defaultdict


def _mad(a, b):
    keys = set(a) | set(b)
    return sum(abs(a.get(k, 0.0) - b.get(k, 0.0)) for k in keys) / len(keys) if keys else 0.0


def compare(path, noul_threshold=0.5):
    stats = defaultdict(lambda: {"n": 0, "top1": [], "noul": [], "mad": []})
    with open(path) as f:
        for line in f:
            if not line.strip():
                continue
            rec = json.loads(line)
            if "secondaries" in rec:
                pairs = [(n, r, True) for n, r in rec["secondaries"].items() if r]
            else:  # old log format
                pairs = [("secondary", rec["secondary"], False)] if rec.get("secondary") else []
            for name, sec, named in pairs:
                _pair(stats, rec["primary"], sec, name if named else None, noul_threshold)
    return dict(stats)


def _pair(stats, primary, sec, name, noul_threshold):
    pa, sa = primary.get("answers", {}), sec.get("answers", {})
    for qid in pa.keys() & sa.keys():
        a, b, s = pa[qid], sa[qid], stats[qid if name is None else "%s [%s]" % (qid, name)]
        s["n"] += 1
        if "choice" in a and "choice" in b:
            s["top1"].append(a["choice"] == b["choice"])
        if "noul" in a and "noul" in b:
            s["noul"].append((a["noul"] >= noul_threshold) == (b["noul"] >= noul_threshold))
            s["mad"].append(abs(a["noul"] - b["noul"]))
        elif "probabilities" in a and "probabilities" in b:
            s["mad"].append(_mad(a["probabilities"], b["probabilities"]))


def _rate(xs):
    return "%.2f" % (sum(xs) / len(xs)) if xs else "-"


def format_table(stats):
    rows = [("question", "n", "top1_agree", "noul_agree", "mean_abs_diff")]
    for qid in sorted(stats):
        s = stats[qid]
        mad = "%.3f" % (sum(s["mad"]) / len(s["mad"])) if s["mad"] else "-"
        rows.append((qid, str(s["n"]), _rate(s["top1"]), _rate(s["noul"]), mad))
    w = [max(len(r[i]) for r in rows) for i in range(5)]
    return "\n".join("  ".join(c.ljust(w[i]) for i, c in enumerate(r)) for r in rows)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: python -m decide_connector.compare <log.jsonl> [noul_threshold]")
    print(format_table(compare(sys.argv[1], float(sys.argv[2]) if len(sys.argv) > 2 else 0.5)))
