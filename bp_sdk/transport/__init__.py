"""bp_sdk.transport — Transport abstraction (WebSocket vs in-process)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from bp_sdk.transport.base import Transport

if TYPE_CHECKING:
    from bp_protocol.types import AgentInfo
    from bp_sdk.settings import AgentConfig


async def build_transport(config: "AgentConfig", *, info: "AgentInfo") -> Transport:
    """Construct the appropriate transport for the configured mode.

    `embedded=True`  → InProcessTransport
    `embedded=False` → WebSocketTransport
    """
    if config.embedded:
        from bp_sdk.transport.inproc import InProcessTransport  # noqa: PLC0415

        return await InProcessTransport.connect(config, info=info)

    from bp_sdk.transport.ws import WebSocketTransport  # noqa: PLC0415

    return await WebSocketTransport.connect(config, info=info)


__all__ = ["Transport", "build_transport"]
