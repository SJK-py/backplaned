# SDK — Core

> Part 1 of the agent SDK design. Covers the agent author surface,
> handler model, transport abstraction, frame dispatch, and lifecycle.
> See [`services.md`](./services.md) for the LLM service, file
> handling, progress, and worked examples.

## 1. Role and design philosophy

The SDK is the **only** code an agent author should need to write
against. Transport (HTTP vs. WebSocket), correlation, ack handling,
heartbeat, reconnection, embedded-vs-external dispatch, and ACL
plumbing all live below the SDK surface. Agent code looks the same
whether the agent runs in-process inside the router or as a separate
container in another datacenter.

Five rules govern the surface:

**S1. Handlers, not endpoints.** Agent authors register typed
coroutines. They never call FastAPI, never write `/receive`, never
touch sockets.

**S2. Typed inputs and outputs.** Every handler declares its accepted
input model and its output model. The SDK validates at the boundary
and raises typed errors before the handler runs.

**S3. One context object.** Every handler receives a `TaskContext`
that exposes everything it needs: cancel token, progress emitter,
file manager, LLM service, peer-call helpers, logger, trace context.
No globals, no singletons reachable from handler code.

**S4. Embedded vs. external is a deployment flag.** The agent author
writes `@agent.handler` once. A config switch decides whether the
agent runs inside the router process or stands up its own WebSocket
client.

**S5. Failures are values.** Handler exceptions become typed `Result`
frames with appropriate status codes. The SDK does not let a stray
exception bring down the agent loop.

## 2. Minimal agent

This is the entire surface required to run a working agent:

```python
from backplaned_sdk import Agent, TaskContext, AgentInfo
from backplaned_sdk.types import LLMData, AgentOutput

agent = Agent(
    info=AgentInfo(
        agent_id="echo",
        description="Echoes the prompt back, in uppercase.",
        capabilities=["text.transform.uppercase"],
        accepts=LLMData,
        produces=AgentOutput,
    ),
)

@agent.handler
async def handle(ctx: TaskContext, payload: LLMData) -> AgentOutput:
    return AgentOutput(content=payload.prompt.upper())

if __name__ == "__main__":
    agent.run()
```

That's it. Onboarding (first run only), reconnect, ack, heartbeat,
trace propagation, ACL hand-off, and graceful shutdown are all
handled by `agent.run()`.

## 3. `Agent` object

The top-level entry point. One per process for external agents; one
per registered agent for embedded agents (multiple may co-exist in the
router process).

```python
class Agent:
    def __init__(
        self,
        info: AgentInfo,
        *,
        config: AgentConfig | None = None,
    ) -> None: ...

    def handler(
        self,
        fn: Callable[[TaskContext, In], Awaitable[Out]],
    ) -> Callable[..., Any]: ...

    def on_startup(self, fn: Callable[[], Awaitable[None]]) -> None: ...
    def on_shutdown(self, fn: Callable[[], Awaitable[None]]) -> None: ...

    def run(self) -> None: ...                    # blocking; for external
    async def run_async(self) -> None: ...        # for embedded
    async def aclose(self) -> None: ...
```

`AgentConfig` is loaded from env vars (`AGENT_*`) by default and
covers: `router_url`, `auth_token`, `embedded`, `transport`,
`reconnect_max_backoff_s`, `pending_results_timeout_s`,
`progress_buffer_size`, `log_level`. Same Pydantic Settings pattern as
the router.

A single `Agent` may register multiple handlers via `@agent.handler`,
keyed by the input model class. Dispatch resolves by `payload`'s
declared schema (set on `NewTask`, see `protocol.md` §2.2). An agent
that handles only one shape needs only one decorator.

## 4. `TaskContext`

The argument every handler receives. Stable surface; new fields are
additive across SDK versions.

```python
class TaskContext:
    task_id: str
    parent_task_id: str | None
    user_id: str
    session_id: str
    trace_id: str
    span_id: str

    cancel_token: CancelToken
    log: structlog.BoundLogger
    progress: ProgressEmitter
    files: ProxyFileManager
    llm: LLMService
    peers: PeerClient

    deadline: datetime | None

    def child_span(self, name: str) -> AbstractContextManager: ...
    def metric(self, name: str, value: float, **labels: str) -> None: ...
```

- `cancel_token` is checked by the SDK in every `await` helper it
  exposes. Handler code that does its own loops should call
  `ctx.cancel_token.raise_if_cancelled()` periodically.
- `log` is pre-bound with `trace_id`, `task_id`, `agent_id`. Every
  log line is automatically correlated.
- `progress` (`services.md` §3) emits `Progress` frames.
- `files` is the per-task `ProxyFileManager` (`services.md` §2),
  scoped to this task's inbox.
- `llm` (`services.md` §1) is the LLM service handle.
- `peers` (§7) lets a handler call other agents.

Construction is internal to the SDK; handlers never instantiate it.

## 5. Transport abstraction

A `Transport` is the layer between the framed protocol and the wire.
Two built-in implementations:

**`WebSocketTransport`** — for external agents. Maintains one
WebSocket to the router. On startup: dial, send `Hello`, await
`Welcome`. On shutdown: drain pending acks, send close. Reconnects
with exponential backoff (jittered) on transport errors. Resume token
(`protocol.md` §3.3) is offered automatically when reconnect occurs
within the resume window.

**`InProcessTransport`** — for embedded agents. Frames are passed via
asyncio `Queue` to the router's dispatch loop in the same process.
No serialization, no network. The router routes outbound frames to
the `InProcessTransport` of the destination embedded agent (or to
its WebSocket if external).

Both implement:

```python
class Transport(Protocol):
    async def send(self, frame: Frame) -> None: ...
    async def recv(self) -> Frame: ...
    async def close(self) -> None: ...
```

Selection happens in `Agent.__init__` from `config.embedded`. Agent
code is unaware of which is in use.

## 6. Frame dispatch and correlation

The SDK runs three coroutines per agent:

1. **Receive loop** — `await transport.recv()`, classify by `type`,
   route to:
   - `NewTask` → handler invocation
   - `Result` for our pending peer calls → resolve correlated Future
   - `Cancel` → trigger cancel token on the matching task
   - `Progress` for our peer calls → forward to subscriber
   - `Ack` → resolve send-side Future
   - `Ping` → respond `Pong`
2. **Send queue drainer** — pulls frames from the agent's outbound
   queue, transmits, registers ack Futures with timeouts.
3. **Heartbeat** — sends `Ping` on idle, fails the socket on missed
   `Pong` (external transport only).

The SDK maintains a `_pending_acks: dict[correlation_id, Future]` and
a `_pending_results: dict[correlation_id, Future]`. The first holds
frame-level send acks; the second holds task-level outcomes for peer
calls. They are independent and time out independently.

Both maps are bounded; orphan entries (peer never replies, socket
dropped before resume) are reaped by a per-second sweep that fails
expired Futures with a typed error.

## 7. Peer calls (`ctx.peers`)

How a handler invokes another agent.

```python
class PeerClient:
    async def spawn(
        self,
        destination_agent_id: str,
        payload: BaseModel,
        *,
        wait: bool = True,
        timeout_s: float | None = None,
    ) -> Result: ...

    async def delegate(
        self,
        destination_agent_id: str,
        payload: BaseModel,
        *,
        handoff_note: str | None = None,
    ) -> None: ...

    async def find(self, capability: str) -> list[AgentInfo]: ...

    async def describe(self, agent_id: str) -> AgentInfo: ...
```

- `spawn` creates a child task. With `wait=True` (default), awaits the
  child `Result`; the SDK manages the correlation. With `wait=False`,
  returns the child task_id and the parent task transitions to
  `WAITING_CHILDREN` until the result arrives via the receive loop.
- `delegate` hands the current task to another agent. The current
  handler should return after delegating; the SDK suppresses the
  default `Result` so the delegated agent is the one to terminate the
  task.
- `find` and `describe` query the router's ACL-aware catalog
  (`state.md` §3.7).

## 8. Lifecycle

```
   process start
        │
        ├── Agent.__init__         (load AgentConfig, build transport)
        │
        ├── on_startup hooks       (user code)
        │
        ├── transport.connect      (WS dial + Hello, or in-process attach)
        │
        ├── receive / send / hb    (concurrent)
        │
        │   … steady state …
        │
        ├── shutdown signal        (SIGTERM / agent.aclose)
        │
        ├── stop accepting NewTask
        ├── drain in-flight handlers
        │   (each gets its cancel_token tripped after grace_s; default 30)
        │
        ├── on_shutdown hooks
        │
        └── transport.close
```

The SDK installs a SIGTERM/SIGINT handler that enters graceful
shutdown. Embedded agents inherit the router's lifespan and shut down
when the router does.

## 9. Onboarding

External agents handle first-run onboarding automatically:

```python
agent.run()  # if no auth_token in AgentConfig:
             #   prompt for invitation_token via env or stdin,
             #   POST /v1/onboard, persist auth_token to disk,
             #   then proceed to connect.
```

Token persistence path is `${AGENT_STATE_DIR}/credentials.json`,
permissions `0600`. Refresh is automatic via
`POST /v1/agent/refresh-token` before expiry.

Embedded agents skip onboarding — the router registers them at
import time using a deployment-trusted in-process credential.

## 10. Errors

Three layers of failure, distinguished:

- **Validation errors** — input doesn't match the handler's accepted
  model. SDK responds with `Result{status:"failed", status_code:400}`
  before invoking the handler.
- **Handler errors** — exception inside user code. SDK catches,
  logs with traceback, responds with
  `Result{status:"failed", status_code:500, error:{...}}`. The agent
  loop continues.
- **Transport errors** — socket dropped, ack timeout, etc. SDK
  triggers reconnect; pending peer calls are failed with
  `transport_error`.

The SDK exposes typed exceptions agent code may raise to control
status codes:

```python
class HandlerError(Exception): status_code = 500
class ValidationError(HandlerError): status_code = 400
class PermissionError(HandlerError): status_code = 403
class NotFoundError(HandlerError): status_code = 404
class CancellationError(HandlerError): status_code = 499
class UpstreamError(HandlerError): status_code = 502
```

Anything that's not one of these is logged at ERROR and surfaces as
`status_code=500`.
