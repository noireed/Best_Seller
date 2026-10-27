from agents.schema import Decision
from risk.guard import validate_order

def execute(client, decision: Decision, *, dry_run=True, **risk_context):
    approved, reason = validate_order(decision, **risk_context)
    if not approved:
        return {"status": "REJECTED", "reason": reason}
    if dry_run:
        return {"status": "DRY_RUN", "decision": decision.model_dump()}
    # Production deployment must reconcile outstanding orders and refresh state.
    return client.place_order(decision.symbol, decision.action, decision.quantity)
