"""bp_sdk.agent — The Agent class that agent authors instantiate."""

from __future__ import annotations

import asyncio
import inspect
import logging
import signal
from dataclasses import dataclass
from typing import (
    Any,
    Awaitable,
    Callable,
    Optional,
    TypeVar,
)

from pydantic import BaseModel

from bp_protocol.types import AgentInfo
from bp_sdk.context import TaskContext
from bp_sdk.errors import HandlerError
from bp_sdk.settings import AgentConfig, load_agent_config

logger = logging.getLogger(__name__)


T_in = TypeVar("T_in", bound=BaseModel)
T_out = TypeVar("T_out", bound=BaseModel)
HandlerFn = Callable[[TaskContext, Any], Awaitable[Any]]


@dataclass
class _RegisteredHandler:
    fn: HandlerFn
    input_model: type[BaseModel]
    output_model: Optional[type[BaseModel]] = None


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class Agent:
    """Top-level entry point for an agent process.

    Usage:

        agent = Agent(info=AgentInfo(...))

        @agent.handler
        async def handle(ctx: TaskContext, payload: LLMData) -> AgentOutput: ...

        agent.run()                # external (blocks)
        await agent.run_async()    # embedded (non-blocking)
    """

    def __init__(
        self,
        info: AgentInfo,
        *,
        config: Optional[AgentConfig] = None,
    ) -> None:
        self.info = info
        self.config = config or load_agent_config()

        self._handlers_by_input: dict[type[BaseModel], _RegisteredHandler] = {}
        self._startup_hooks: list[Callable[[], Awaitable[None]]] = []
        self._shutdown_hooks: list[Callable[[], Awaitable[None]]] = []

        # Filled on connect by the dispatch module.
        self._dispatcher: Optional[Any] = None
        self._stop_event: asyncio.Event = asyncio.Event()

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def handler(self, fn: HandlerFn) -> HandlerFn:
        """Register `fn` as a handler. Input model is inferred from the
        2nd positional arg's type annotation; must subclass BaseModel."""
        if not inspect.iscoroutinefunction(fn):
            raise TypeError(
                f"{fn.__qualname__} must be async — embedded agents in particular "
                "rely on this for event-loop fairness."
            )
        sig = inspect.signature(fn)
        params = list(sig.parameters.values())
        if len(params) < 2:
            raise TypeError("handler must take (ctx, payload) — saw fewer params")
        input_model = params[1].annotation
        if not (isinstance(input_model, type) and issubclass(input_model, BaseModel)):
            raise TypeError(
                f"{fn.__qualname__} payload type must be a Pydantic BaseModel subclass; "
                f"got {input_model!r}"
            )
        output_model = sig.return_annotation if sig.return_annotation is not sig.empty else None
        self._handlers_by_input[input_model] = _RegisteredHandler(
            fn=fn,
            input_model=input_model,
            output_model=output_model
            if isinstance(output_model, type) and issubclass(output_model, BaseModel)
            else None,
        )
        return fn

    def on_startup(self, fn: Callable[[], Awaitable[None]]) -> None:
        self._startup_hooks.append(fn)

    def on_shutdown(self, fn: Callable[[], Awaitable[None]]) -> None:
        self._shutdown_hooks.append(fn)

    # ------------------------------------------------------------------
    # Internal: dispatch resolution
    # ------------------------------------------------------------------

    def resolve_handler(self, input_model: type[BaseModel]) -> Optional[_RegisteredHandler]:
        return self._handlers_by_input.get(input_model)

    @property
    def registered_handlers(self) -> dict[type[BaseModel], _RegisteredHandler]:
        return self._handlers_by_input

    # ------------------------------------------------------------------
    # Run loops
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Blocking run loop for external agents. Installs SIGINT/SIGTERM."""
        try:
            asyncio.run(self.run_async())
        except KeyboardInterrupt:
            pass

    async def run_async(self) -> None:
        """Async run loop. Suitable for embedded agents (router lifespan)."""
        from bp_sdk.dispatch import build_dispatcher  # noqa: PLC0415
        from bp_sdk.transport import build_transport  # noqa: PLC0415

        # Onboard if needed
        if not self.config.embedded and not self.config.auth_token:
            from bp_sdk.onboarding import onboard_or_resume  # noqa: PLC0415

            await onboard_or_resume(self.info, self.config)

        transport = await build_transport(self.config, info=self.info)
        self._dispatcher = build_dispatcher(self, transport)

        for hook in self._startup_hooks:
            await hook()

        # Install signal handlers (best-effort; not available on Windows main loop)
        try:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, self._stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass

        # Proactive token refresh — only meaningful for external agents
        # (embedded agents inherit the router's trust boundary and don't
        # carry their own JWT).
        refresh_task: Optional[asyncio.Task] = None
        if not self.config.embedded:
            refresh_task = asyncio.create_task(self._token_refresh_loop())

        try:
            await self._dispatcher.run_until(self._stop_event)  # type: ignore[union-attr]
        finally:
            if refresh_task is not None:
                refresh_task.cancel()
                try:
                    await refresh_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            for hook in self._shutdown_hooks:
                try:
                    await hook()
                except Exception:  # noqa: BLE001
                    logger.exception("shutdown_hook_failed")
            await transport.close()

    async def _token_refresh_loop(self) -> None:
        """Refresh the agent JWT proactively before expiry.

        Wakes at `schedule_seconds_until_refresh(token)`, rotates via
        `bp_sdk.onboarding.refresh_token`, then loops on the new token.
        On failure, retries with exponential backoff capped at 5 min.
        Cancellation (agent shutdown) propagates as asyncio.CancelledError.
        """
        from bp_sdk.onboarding import (  # noqa: PLC0415
            refresh_token,
            schedule_seconds_until_refresh,
        )

        backoff_s = 30.0
        while not self._stop_event.is_set():
            token = self.config.auth_token
            if not token:
                # Should not happen post-onboard, but be defensive.
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=60.0)
                except asyncio.TimeoutError:
                    continue
                return

            sleep_s = schedule_seconds_until_refresh(token)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=sleep_s)
                return  # stop_event fired
            except asyncio.TimeoutError:
                pass

            try:
                new_exp = await refresh_token(self.config)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "token_refresh_unexpected", extra={"event": "token_refresh_unexpected"}
                )
                new_exp = None

            if new_exp is None:
                # Transient — back off and retry. Capped so we don't sleep
                # past the token's actual expiry without trying again.
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=backoff_s)
                    return
                except asyncio.TimeoutError:
                    pass
                backoff_s = min(backoff_s * 2, 300.0)
            else:
                backoff_s = 30.0  # success — reset

    async def aclose(self) -> None:
        self._stop_event.set()


# Re-exported for convenience
__all__ = ["Agent", "HandlerError"]
