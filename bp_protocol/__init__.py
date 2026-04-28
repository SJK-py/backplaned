"""bp_protocol — Shared frame and type definitions for the reworked Backplaned.

This package is the single source of truth for the wire protocol between
router and agents. Both `bp_router` and `bp_sdk` depend on it.

See `docs/router/protocol.md` for the protocol specification and
`docs/overview.md` for the overall architecture.
"""

from bp_protocol.frames import (
    AckFrame,
    CancelFrame,
    ErrorCode,
    ErrorFrame,
    Frame,
    HelloFrame,
    NewTaskFrame,
    PingFrame,
    PongFrame,
    ProgressFrame,
    ResultFrame,
    WelcomeFrame,
    parse_frame,
    serialize_frame,
)
from bp_protocol.types import (
    AgentInfo,
    AgentOutput,
    LLMCall,
    LLMData,
    ProxyFile,
    ProxyFileProtocol,
    TaskPriority,
    TaskState,
    TaskStatus,
)

PROTOCOL_VERSION = "1"

__all__ = [
    "PROTOCOL_VERSION",
    # types
    "AgentInfo",
    "AgentOutput",
    "LLMCall",
    "LLMData",
    "ProxyFile",
    "ProxyFileProtocol",
    "TaskPriority",
    "TaskState",
    "TaskStatus",
    # frames
    "AckFrame",
    "CancelFrame",
    "ErrorCode",
    "ErrorFrame",
    "Frame",
    "HelloFrame",
    "NewTaskFrame",
    "PingFrame",
    "PongFrame",
    "ProgressFrame",
    "ResultFrame",
    "WelcomeFrame",
    "parse_frame",
    "serialize_frame",
]
