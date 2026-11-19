import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from decide_connector.adapt import preflight
from decide_connector.engines import PeeweeLocalEngine

state = {"subject": "Billed twice", "body": "I was charged twice for my March invoice, please refund one."}
questions = {
    "is_billing": {"type": "noul", "instructions": "Is this ticket about billing?"},
    "dept": {"type": "choice", "instructions": "Which department should handle this ticket?",
             "criteria": {"billing": "payments, invoices, refunds", "support": "how-to and product help",
                          "sales": "pricing and new purchases"}},
}
q, w = preflight("peewee", questions)
print("preflight warnings:", w)
print(json.dumps(PeeweeLocalEngine().decide(state, q), indent=1))
