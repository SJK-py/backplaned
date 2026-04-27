"""bp_sdk.transport.base — Transport Protocol."""

from __future__ import annotations

from typing import Protocol

from bp_protocol.frames import Frame


class Transport(Protocol):
    """Layer between the framed protocol and the wire.

    Implementations: WebSocketTransport, InProcessTransport. Both are
    full-duplex and frame-oriented. Reconnection, heartbeat, and resume
    are implementation responsibilities — not surfaced to callers.
    """

    async def send(self, frame: Frame) -> None:
        ...

    async def recv(self) -> Frame:
        ...

    async def close(self) -> None:
        ...

    @property
    def is_connected(self) -> bool:
        ...
