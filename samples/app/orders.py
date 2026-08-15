"""Order handling for the sample backend.

Every function here has a real, deliberate defect. They are written the way
such bugs actually appear — a missing guard, an unvalidated key, an assumed
non-null — rather than as obvious `raise` statements, so the LLM has to read
the code to work out what went wrong.
"""

from __future__ import annotations

from typing import Any

# Pretend inventory. `quantity` of 0 is legal here and that is the whole problem.
CATALOG: dict[str, dict[str, Any]] = {
    "SKU-1": {"name": "Standing desk", "quantity": 4, "total_cents": 89_600},
    "SKU-2": {"name": "Monitor arm", "quantity": 0, "total_cents": 0},
}


def create_order(sku: str) -> dict[str, Any]:
    """Build an order line and derive the per-unit price.

    Defect: `quantity` is never checked for zero before the division.
    """
    item = CATALOG[sku]
    total = item["total_cents"]
    quantity = item["quantity"]

    unit_price = total / quantity  # ZeroDivisionError when quantity == 0

    return {"sku": sku, "unit_price_cents": unit_price, "quantity": quantity}


def apply_discount(payload: dict[str, Any]) -> dict[str, Any]:
    """Apply a promo code to an order.

    Defect: reads a key straight off the request body with no validation.
    """
    code = payload["discount_code"]  # KeyError when the client omits it
    return {"code": code, "applied": True}


def summarize_order(order: dict[str, Any]) -> str:
    """One-line summary for the confirmation email.

    Defect: `customer` is nullable for guest checkout, but treated as a string.
    """
    customer = order.get("customer")
    return f"Order for {customer.upper()}"  # AttributeError when customer is None


def recalculate_order(sku: str) -> dict[str, Any]:
    """Recompute a line after an inventory sync.

    Defect: this is the *same* defect as `create_order` — dividing by a
    quantity that is allowed to be zero — reached through a different function
    and a different line. A fingerprint hash treats this as a brand-new bug.
    Semantic matching should recognise it as the one we already know about.
    """
    item = CATALOG[sku]
    subtotal = item["total_cents"]
    item_count = item["quantity"]

    return {"sku": sku, "unit_price_cents": subtotal / item_count}
