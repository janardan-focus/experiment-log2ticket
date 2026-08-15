"""Payment gateway calls for the sample backend.

Note the hardcoded credential below. It is fake, and it is here on purpose:
this module's source gets pulled into the LLM context when a payment frame
appears in a traceback, so it is the test case for whether redaction actually
runs before anything leaves the process.
"""

from __future__ import annotations

from typing import Any

# Deliberately bad practice — this is the redaction test fixture.
GATEWAY_API_KEY = "sk_test_FAKEKEYnotreal0000000"
GATEWAY_DSN = "postgresql://payments_user:hunter2@db.internal:5432/payments"
SUPPORT_CONTACT = "payments-oncall@example.com"


class UpstreamTimeout(Exception):
    """Raised when the payment gateway does not answer in time."""


def charge(order_id: str, amount_cents: int) -> dict[str, Any]:
    """Charge a card through the upstream gateway.

    Defect: no timeout budget, no retry, and the failure surfaces as an
    unhandled 500 rather than something the caller can act on. This is the
    'looks transient, might not deserve a ticket' case.
    """
    # Bound to locals on purpose: these are live in the frame at the moment the
    # exception is raised, so they are what CAPTURE_LOCALS would send to the
    # model. This is the redaction test.
    api_key = GATEWAY_API_KEY
    dsn = GATEWAY_DSN
    notify = SUPPORT_CONTACT

    _authenticate(api_key)

    raise UpstreamTimeout(
        f"gateway did not respond within 30s while charging order {order_id} "
        f"for {amount_cents} cents (notify {notify}, dsn {dsn})"
    )


def _authenticate(api_key: str) -> None:
    if not api_key.startswith("sk_"):
        raise ValueError("malformed gateway key")
