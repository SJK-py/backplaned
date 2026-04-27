"""bp_sdk.correlation — SDK-side pending acks and pending peer results."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class _Pending:
    future: asyncio.Future[Any]
    deadline: float = field(default=0.0)


class PendingMap:
    """Generic correlation_id → Future map with timeout reaping.

    Late-arriving resolve()s for keys that have not been registered yet
    are buffered for a short window so a subsequent register() picks
    them up immediately. This handles the unavoidable race when the
    receive loop processes a result before the awaiting coroutine has
    registered its future (e.g. after a multi-step spawn → ack →
    register-result-future flow).
    """

    BUFFER_RESOLVES_S = 5.0

    def __init__(self, *, default_timeout_s: float) -> None:
        self._pending: dict[str, _Pending] = {}
        self._buffered: dict[str, tuple[Any, float]] = {}
        self._default_timeout = default_timeout_s
        self._reaper: Optional[asyncio.Task] = None

    def register(
        self, correlation_id: str, *, timeout_s: Optional[float] = None
    ) -> asyncio.Future:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()

        # If a value was buffered for this id, hand it back immediately.
        buffered = self._buffered.pop(correlation_id, None)
        if buffered is not None:
            value, _ts = buffered
            if isinstance(value, BaseException):
                fut.set_exception(value)
            else:
                fut.set_result(value)
            return fut

        self._pending[correlation_id] = _Pending(
            fut, loop.time() + (timeout_s or self._default_timeout)
        )
        return fut

    def resolve(self, correlation_id: str, value: Any) -> bool:
        entry = self._pending.pop(correlation_id, None)
        if entry is None:
            # Buffer for late register().
            loop = asyncio.get_event_loop()
            self._buffered[correlation_id] = (value, loop.time())
            return False
        if not entry.future.done():
            entry.future.set_result(value)
        return True

    def reject(self, correlation_id: str, exc: BaseException) -> bool:
        entry = self._pending.pop(correlation_id, None)
        if entry is None:
            self._buffered[correlation_id] = (exc, asyncio.get_event_loop().time())
            return False
        if not entry.future.done():
            entry.future.set_exception(exc)
        return True

    def reject_all(self, exc: BaseException) -> int:
        rejected = 0
        for cid in list(self._pending):
            if self.reject(cid, exc):
                rejected += 1
        return rejected

    def start_reaper(self) -> None:
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.create_task(self._reap())

    async def _reap(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            try:
                await asyncio.sleep(1.0)
                now = loop.time()
                expired = [cid for cid, p in self._pending.items() if p.deadline <= now]
                for cid in expired:
                    self.reject(cid, TimeoutError("correlation_timeout"))
                # Drop buffered entries older than BUFFER_RESOLVES_S.
                stale = [
                    cid
                    for cid, (_, ts) in self._buffered.items()
                    if now - ts > self.BUFFER_RESOLVES_S
                ]
                for cid in stale:
                    self._buffered.pop(cid, None)
            except asyncio.CancelledError:
                return
