"""Order service fixture for a refactor task.

The email-validation logic currently lives inline inside ``create_order``.
The agent is expected to extract it into a top-level ``validate_email``
function so it can be reused (and unit-tested) without going through
``create_order``.
"""

from __future__ import annotations


def create_order(customer_email: str, items: list[dict[str, int]]) -> dict[str, object]:
    """Create an order dict for a customer, validating the email inline.

    Returns a dict with ``status`` == ``"ok"`` on success or
    ``status`` == ``"error"`` with a ``reason`` on failure.
    """
    # Inline email validation — extract me into validate_email().
    if not customer_email or "@" not in customer_email:
        return {"status": "error", "reason": "invalid email"}
    local, _, domain = customer_email.partition("@")
    if not local or not domain or "." not in domain:
        return {"status": "error", "reason": "invalid email"}

    if not items:
        return {"status": "error", "reason": "empty cart"}

    total = sum(item.get("qty", 0) * item.get("price", 0) for item in items)
    return {
        "status": "ok",
        "customer_email": customer_email,
        "items": items,
        "total": total,
    }
