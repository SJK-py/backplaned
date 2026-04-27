"""bp_sdk.errors — Typed exceptions agent code may raise.

Each error maps to a specific status_code on the Result frame. See
`docs/design/sdk/core.md` §10.
"""

from __future__ import annotations


class HandlerError(Exception):
    """Base class. Maps to status_code=500 unless subclass overrides."""

    status_code: int = 500


class ValidationError(HandlerError):
    """Input failed schema validation. Maps to 400."""

    status_code = 400


class PermissionError(HandlerError):
    status_code = 403


class NotFoundError(HandlerError):
    status_code = 404


class CancellationError(HandlerError):
    """Raised by `ctx.cancel_token` when a task is cancelled. Maps to 499."""

    status_code = 499


class UpstreamError(HandlerError):
    """Wraps an error from a downstream call (LLM, peer, storage). Maps to 502."""

    status_code = 502


class TransportError(Exception):
    """Socket-level failure (disconnect, ack timeout). Not a handler error;
    handled by the SDK loop, not by user code."""


class ProtocolError(Exception):
    """Frame-level invariant violation (unexpected frame, version mismatch)."""
