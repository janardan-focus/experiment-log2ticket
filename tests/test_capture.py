from __future__ import annotations

import json

from log2ticket.capture import capture_exception, exception_chain, is_library_path
from log2ticket.config import Settings
from log2ticket.store import IncidentStore


def _settings(**overrides) -> Settings:
    return Settings(github_token="", github_repo="", **overrides)


def _raise_zero_division() -> None:
    total = 100
    quantity = 0
    _ = total / quantity


def test_captures_type_message_and_frames():
    try:
        _raise_zero_division()
    except ZeroDivisionError as exc:
        event = capture_exception(exc, settings=_settings())

    assert event.exception_type == "ZeroDivisionError"
    assert "division by zero" in event.exception_message
    assert event.frames[-1].function == "_raise_zero_division"
    assert event.frames[-1].in_repo is True
    assert event.incident_id


def test_locals_are_off_by_default():
    try:
        _raise_zero_division()
    except ZeroDivisionError as exc:
        event = capture_exception(exc, settings=_settings())

    assert all(frame.locals_repr is None for frame in event.frames)


def test_locals_captured_when_enabled():
    try:
        _raise_zero_division()
    except ZeroDivisionError as exc:
        event = capture_exception(exc, settings=_settings(capture_locals=True))

    locals_at_failure = event.frames[-1].locals_repr
    assert locals_at_failure is not None
    assert locals_at_failure["quantity"] == "0"
    assert locals_at_failure["total"] == "100"


def test_capture_does_not_truncate_to_the_display_limit():
    """Truncation moved to ContextAssembler, after sanitizing.

    Clipping here would cut the terminator that sanitizing rules anchor on, so
    capture keeps the full value and only applies a coarse memory guard.
    See test_review_regressions.test_long_secret_is_sanitized_before_truncation.
    """
    def boom() -> None:
        payload = "x" * 5_000  # noqa: F841
        raise ValueError("nope")

    try:
        boom()
    except ValueError as exc:
        event = capture_exception(exc, settings=_settings(capture_locals=True,
                                                          max_local_repr_chars=50))

    payload = event.frames[-1].locals_repr["payload"]
    assert len(payload) > 50, "capture must not pre-truncate"


def test_locals_count_is_capped():
    def boom() -> None:
        a, b, c, d, e = 1, 2, 3, 4, 5  # noqa: F841
        raise ValueError("nope")

    try:
        boom()
    except ValueError as exc:
        event = capture_exception(exc, settings=_settings(capture_locals=True,
                                                          max_locals_per_frame=2))

    assert len(event.frames[-1].locals_repr) == 2


def test_chained_exception_is_recorded_as_context():
    """The exception we hold is the outermost; the chain points inward."""
    try:
        try:
            raise KeyError("discount_code")
        except KeyError as inner:
            raise ValueError("could not price order") from inner
    except ValueError as exc:
        event = capture_exception(exc, settings=_settings())

    assert event.exception_type == "ValueError"
    assert len(event.chained_from) == 1
    assert "KeyError" in event.chained_from[0]


def test_self_referential_chain_terminates():
    exc = ValueError("loop")
    exc.__cause__ = exc
    assert exception_chain(exc) == []


def test_raise_from_none_suppresses_context():
    try:
        try:
            raise KeyError("hidden")
        except KeyError:
            raise ValueError("visible") from None
    except ValueError as exc:
        event = capture_exception(exc, settings=_settings())

    assert event.chained_from == []


def test_library_paths_detected():
    assert is_library_path("/usr/lib/python3.13/json/decoder.py")
    assert is_library_path("/venv/lib/site-packages/fastapi/routing.py")
    assert is_library_path("<frozen importlib._bootstrap>")
    assert not is_library_path("/app/samples/app/orders.py")


def test_library_frames_carry_no_locals():
    """json.loads raises inside the stdlib; that frame must stay opaque."""
    try:
        json.loads("{not json")
    except json.JSONDecodeError as exc:
        event = capture_exception(exc, settings=_settings(capture_locals=True))

    library_frames = [f for f in event.frames if not f.in_repo]
    assert library_frames, "expected at least one stdlib frame"
    assert all(f.locals_repr is None for f in library_frames)


def test_endpoint_is_recorded():
    try:
        _raise_zero_division()
    except ZeroDivisionError as exc:
        event = capture_exception(exc, settings=_settings(), endpoint="POST /boom/x")

    assert event.endpoint == "POST /boom/x"


def test_store_is_bounded_and_newest_last():
    store = IncidentStore(maxlen=3)
    ids = []
    for _ in range(5):
        try:
            _raise_zero_division()
        except ZeroDivisionError as exc:
            ids.append(store.add(capture_exception(exc, settings=_settings())))

    assert len(store) == 3
    assert store.latest().incident_id == ids[-1]
    assert store.get(ids[0]) is None      # evicted
    assert store.get(ids[-1]) is not None
