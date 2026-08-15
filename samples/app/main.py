"""Sample backend: the thing that breaks, plus the demo UI.

This stands in for a real FastAPI service. The global exception handler is the
same shape the Log2Ticket proposal describes — one place catches every
unhandled exception — except that instead of formatting a traceback to a log
file, it hands the live exception straight to `capture_exception`.

Nothing is written to disk.
"""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from log2ticket_triage.capture import capture_exception  # noqa: E402
from log2ticket_triage.config import get_settings  # noqa: E402
from log2ticket_triage.context import ContextAssembler  # noqa: E402
from log2ticket_triage.store import IncidentStore  # noqa: E402
from log2ticket_triage.ticket_writer import TicketWriter  # noqa: E402

from . import orders, payments  # noqa: E402

settings = get_settings()
STATIC_DIR = Path(__file__).parent / "static"

store = IncidentStore(maxlen=settings.incident_buffer_size)
assembler = ContextAssembler(settings)
ticket_writer = TicketWriter(settings)

app = FastAPI(title="Log2Ticket sample backend")


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Capture the live exception. No serialising, no file, no parsing.

    This is the push-side entry point — in a real service this handler is where
    the log2ticket library gets called.
    """
    event = capture_exception(
        exc,
        settings=settings,
        endpoint=f"{request.method} {request.url.path}",
    )
    store.add(event)

    return JSONResponse(
        status_code=500,
        content={
            "detail": "Internal server error",
            "incident_id": event.incident_id,
            "exception": event.exception_type,
            "frames_captured": len(event.frames),
            "locals_captured": settings.capture_locals,
        },
    )


# --- The UI ------------------------------------------------------------------


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/mode")
async def mode() -> dict[str, object]:
    """So the UI can show its badges without guessing."""
    return {
        "dry_run": settings.dry_run,
        "model": settings.llm_model,
        "repo": settings.github_repo or "(unset)",
        "capture_locals": settings.capture_locals,
        "incidents": len(store),
    }


# --- Ticket writing ----------------------------------------------------------


@app.post("/triage/inspect")
async def triage_inspect() -> JSONResponse:
    """Show exactly what the model would be sent. Free — no API key needed."""
    event = store.latest()
    if event is None:
        return JSONResponse(
            status_code=404,
            content={
                "error": "No incidents captured yet",
                "detail": "Trigger one of the buttons on the left first.",
            },
        )

    context = assembler.build(event)
    return JSONResponse(
        content={
            "incident_id": event.incident_id,
            "context_text": context.to_prompt_block(),
            "redactions": assembler.report(event),
        }
    )


@app.post("/triage")
async def triage() -> JSONResponse:
    """Full run: assemble context, search existing issues, write the ticket."""
    event = store.latest()
    if event is None:
        return JSONResponse(
            status_code=404,
            content={
                "error": "No incidents captured yet",
                "detail": "Trigger one of the buttons on the left first.",
            },
        )

    result = await ticket_writer.run(event)

    payload: dict[str, object] = {
        "incident_id": event.incident_id,
        "context_text": result.context.to_prompt_block(),
        "redactions": assembler.report(event),
        "searched": "\n".join(result.tool_calls) or None,
        "guardrails": result.guardrails,
        "dry_run": settings.dry_run,
    }

    if result.error:
        # A failed triage degrades to a message, never to a crashed request —
        # the context above is still worth showing.
        payload["error"] = "Triage failed"
        payload["detail"] = result.error
        return JSONResponse(status_code=502, content=payload)

    payload["ticket"] = result.ticket.model_dump() if result.ticket else None
    return JSONResponse(content=payload)


# --- Things that break -------------------------------------------------------
# Each of these is a real failure, not a raise. The stack points at the actual
# defective line in orders.py / payments.py.


@app.post("/boom/zero-division")
async def boom_zero_division() -> dict[str, object]:
    """SKU-2 has quantity 0. create_order divides by it."""
    return orders.create_order("SKU-2")


@app.post("/boom/key-error")
async def boom_key_error() -> dict[str, object]:
    """Client omitted discount_code; apply_discount reads it unguarded."""
    return orders.apply_discount({"sku": "SKU-1", "quantity": 2})


@app.post("/boom/attribute-error")
async def boom_attribute_error() -> dict[str, str]:
    """Guest checkout leaves customer as None; summarize_order calls .upper()."""
    return {"summary": orders.summarize_order({"sku": "SKU-1", "customer": None})}


@app.post("/boom/timeout")
async def boom_timeout() -> dict[str, object]:
    """Upstream gateway never answers. Looks transient — should it be a ticket?"""
    return payments.charge(order_id="ord_8812", amount_cents=4_999)


@app.post("/boom/repeat-bug")
async def boom_repeat_bug() -> dict[str, object]:
    """The same divide-by-zero defect, different function and line.

    A fingerprint hash sees a brand-new bug. Semantic matching should tie it
    back to the one /boom/zero-division already reported.
    """
    return orders.recalculate_order("SKU-2")
