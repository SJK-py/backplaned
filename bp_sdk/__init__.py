"""bp_sdk — Python SDK for writing agents against the bp_router.

See `docs/design/sdk/core.md` and `docs/design/sdk/services.md`.

The expected agent surface is small:

    from bp_sdk import Agent, TaskContext
    from bp_protocol import AgentInfo, AgentOutput, LLMData

    agent = Agent(info=AgentInfo(...))

    @agent.handler
    async def handle(ctx: TaskContext, payload: LLMData) -> AgentOutput:
        ...

    if __name__ == "__main__":
        agent.run()
"""

from bp_sdk.agent import Agent
from bp_sdk.context import TaskContext
from bp_sdk.errors import (
    CancellationError,
    HandlerError,
    NotFoundError,
    PermissionError,
    UpstreamError,
    ValidationError,
)
from bp_sdk.settings import AgentConfig, load_agent_config

__all__ = [
    "Agent",
    "AgentConfig",
    "CancellationError",
    "HandlerError",
    "NotFoundError",
    "PermissionError",
    "TaskContext",
    "UpstreamError",
    "ValidationError",
    "load_agent_config",
]
