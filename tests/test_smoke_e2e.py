"""End-to-end smoke test exercising the happy path.

Boots an in-process bp_router (TestRouter), stands up a real external
agent (bp_sdk.Agent over WebSocket), drives admit_task → NewTask
delivery → handler invocation → Result fan-out via TestRouter.call(),
and asserts the round-trip output.

Skipped when TEST_DB_URL is not set.
"""

from __future__ import annotations

import asyncio

import pytest

from bp_protocol.types import AgentInfo, AgentOutput, LLMData
from bp_sdk import Agent, TaskContext
from bp_sdk.settings import AgentConfig
from bp_sdk.testing import TestRouter


pytestmark = pytest.mark.asyncio


async def test_echo_handler_round_trip(test_db_url: str, tmp_path) -> None:
    info = AgentInfo(
        agent_id="echo_uppercaser",
        description="Echoes the prompt back, in uppercase.",
        capabilities=["text.transform.uppercase"],
        accepts_schema={"type": "object", "properties": {"prompt": {"type": "string"}}},
    )

    user_state_dir = tmp_path / "agent_state"

    async with TestRouter(db_url=test_db_url) as router:
        # Register the agent and grab its JWT.
        token = await router.register_agent(info)

        # Boot the SDK agent against the running router.
        config = AgentConfig(
            embedded=False,
            router_url=router.ws_url,
            state_dir=user_state_dir,
            auth_token=token,
            pending_acks_timeout_s=10.0,
            pending_results_timeout_s=15.0,
        )
        agent = Agent(info=info, config=config)

        @agent.handler
        async def handle(ctx: TaskContext, payload: LLMData) -> AgentOutput:
            return AgentOutput(content=payload.prompt.upper())

        run_task = asyncio.create_task(agent.run_async())

        # Give the agent a moment to connect.
        for _ in range(50):
            await asyncio.sleep(0.05)
            if agent._dispatcher and agent._dispatcher.transport.is_connected:
                break

        try:
            user = await router.create_user(role="user")
            result = await router.call(
                info.agent_id,
                LLMData(prompt="hello world"),
                user_id=user.user_id,
            )

            assert result.status.value == "succeeded"
            assert result.status_code == 200
            assert result.output is not None
            assert result.output.content == "HELLO WORLD"
        finally:
            await agent.aclose()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(run_task, timeout=5.0)
