# SDK — Services and Examples

> Part 2 of the agent SDK design. Covers the LLM service, ProxyFile
> management, progress emission, cancellation, tool builders, embedded
> vs. external deployment, testing helpers, and worked agent examples.
> See [`core.md`](./core.md) for the agent surface and dispatch model.

## 1. LLM service (`ctx.llm`)

The LLM bridge is promoted from "embedded agent" to a first-class
SDK service. Handlers call it directly, never through a peer
`spawn`. Centralised here for telemetry, quota enforcement, caching,
and consistent error mapping across providers.

```python
class LLMService:
    async def generate(
        self,
        prompt: str | list[Message],
        *,
        model: str = "default",
        tools: list[ToolSpec] | None = None,
        tool_choice: ToolChoice | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        stream: bool = False,
        provider_options: dict[str, Any] | None = None,
    ) -> LLMResponse | AsyncIterator[LLMDelta]: ...

    async def embed(self, text: str | list[str], *,
                    model: str = "default") -> list[list[float]]: ...

    async def count_tokens(self, prompt: str | list[Message], *,
                           model: str = "default") -> int: ...
```

### 1.1 Provider routing

`model` is a deployment-config alias (`"default"`, `"fast"`,
`"reasoning"`, `"gemini-2.5"`, `"claude-haiku"`, etc.). The router's
LLM service maps aliases to provider + concrete model + credentials.
Agent code never sees raw API keys, never picks providers by name.

### 1.2 Streaming

`stream=True` returns an `AsyncIterator[LLMDelta]`. The SDK plumbs
deltas straight into the active task's `Progress` channel
automatically (event type `chunk`), so UI clients receive tokens
without extra agent code:

```python
async for delta in await ctx.llm.generate(prompt, stream=True):
    # deltas are also auto-forwarded as Progress(chunk) frames;
    # explicit handling is optional.
    if delta.tool_call:
        ...
```

### 1.3 Tool calls

Tool definitions use a provider-neutral shape (`ToolSpec`). The
service translates to provider-native formats. Tool call results are
typed:

```python
class LLMResponse:
    text: str
    tool_calls: list[ToolCall]
    finish_reason: Literal["stop","length","tool_calls","content_filter"]
    usage: TokenUsage
    raw: dict[str, Any]                # provider-specific extras
```

For Gemini-native features (grounding, code execution, URL context,
image/video generation), use `provider_options`:

```python
await ctx.llm.generate(
    prompt,
    model="gemini-2.5",
    provider_options={
        "tools": [{"google_search": {}}, {"code_execution": {}}],
        "thinking_budget_tokens": 8192,
    },
)
```

`provider_options` is opaque to the SDK; the LLM service forwards it
as-is to the provider client. This is the deliberate escape hatch for
provider-tailored agents — keep the typed surface narrow, let the
provider-specific blob carry capabilities the neutral shape doesn't
cover.

### 1.4 Quota and budget

Calls fail fast with `quota_exceeded` if the user's budget for the
selected model is depleted. `LLMResponse.usage` is reported back to
the router so quotas update in near-real-time.

## 2. File handling (`ctx.files`)

Evolution of today's `ProxyFileManager` (`helper.py:290-628`). Same
inbound/outbound model; tighter API surface.

```python
class ProxyFileManager:
    async def fetch(self, pf: ProxyFile) -> Path: ...
    async def fetch_all(self, files: list[ProxyFile]) -> list[Path]: ...

    async def put(self, src: Path | bytes | AsyncIterable[bytes], *,
                  filename: str | None = None,
                  mime_type: str | None = None) -> ProxyFile: ...

    def resolve(self, local_path: Path | str) -> ProxyFile | None: ...
    def list(self) -> dict[Path, ProxyFile]: ...
```

### 2.1 Differences from current

- `fetch` returns `pathlib.Path`, not `str`.
- `put` is the new outbound primitive — explicit, not inferred from
  argument scanning. The current `resolve_in_args()` heuristic
  (`helper.py:559`) is removed; agents declare files explicitly in
  their output models.
- Embedded agents get the same surface; the SDK chooses
  `localfile` or `presigned` ProxyFile representation based on the
  router's configured `FileStore`.
- `_serve_keys` (`helper.py:337`) goes away — external agents no
  longer serve files via HTTP. They `put` directly to the router's
  storage backend (presigned URL where supported, multipart upload
  otherwise).

### 2.2 Lifecycle

The per-task `ProxyFileManager` cleans up its inbox on task
completion (success or failure). Files put via `ctx.files.put()`
inherit the task's TTL by default. Long-lived files (e.g. user
uploads) are owned by the session, not the task — `ctx.session.files`
is a separate manager for that scope (planned).

## 3. Progress (`ctx.progress`)

```python
class ProgressEmitter:
    async def emit(self, event: str, content: str = "",
                   **metadata: Any) -> None: ...
    def chunk(self, text: str) -> None: ...        # token streaming
    def status(self, status: str) -> None: ...     # human-readable
    def tool_call(self, name: str, args: dict) -> None: ...
    def tool_result(self, name: str, result: Any) -> None: ...
```

`emit` is best-effort; backpressure causes `chunk` events to be
coalesced (multi-token concatenation) and oldest non-chunk events to
be dropped if the per-socket outbox is full
(`protocol.md` §4.5). Returns immediately — no agent code is ever
blocked on a slow consumer.

## 4. Cancellation

Cancellation arrives as a router-issued `Cancel` frame and surfaces
in the SDK as a tripped `cancel_token`:

```python
@agent.handler
async def handle(ctx: TaskContext, payload: LLMData) -> AgentOutput:
    for chunk in long_iterable():
        ctx.cancel_token.raise_if_cancelled()
        ...
```

The cancel token is also wired into the SDK's internal `await`
helpers (`ctx.llm.generate`, `ctx.peers.spawn`, `ctx.files.fetch`),
so cooperative agent code that does most of its waiting via SDK
helpers gets cancellation for free. Pure CPU loops must check the
token themselves.

`CancellationError` from `core.md` §10 is what the SDK raises and
maps to `Result{status:"cancelled", status_code:499}`.

## 5. Tool builders

Replaces today's `build_anthropic_tools` / `build_openai_tools`
(`helper.py:1051,1104`). Same idea, more providers, less
provider-specific code paths in agents:

```python
from backplaned_sdk.tools import build_tools

tools = build_tools(
    destinations=ctx.peers.visible(),       # ACL-filtered
    provider="anthropic",                   # |"openai"|"gemini"
)
```

`build_tools()` consults a registry of provider adapters; new
providers are added by registering a `ToolFormatAdapter`, not by
forking a function. The hidden flag (`AgentInfo.hidden`,
`helper.py:173`) is preserved.

## 6. Embedded vs. external

Same handler code, different runtime. The choice is made in
deployment config:

```toml
# router config — register an embedded agent
[[router.embedded_agents]]
module = "my_agents.echo:agent"
```

```toml
# external agent — its own process
[agent]
embedded = false
router_url = "wss://router.example.com/v1/agent"
```

What changes under the hood:

| Concern              | Embedded                                | External                          |
| -------------------- | --------------------------------------- | --------------------------------- |
| Transport            | `InProcessTransport` (asyncio queues)   | `WebSocketTransport`              |
| Handler dispatch     | Direct async function call              | Frame → recv loop → dispatch      |
| File representation  | `localfile` (shared filesystem)         | `presigned` or `router-proxy`     |
| Auth                 | Implicit, in-process trust              | Bearer JWT over WS                |
| Crash blast radius   | Takes router with it                    | Isolated process                  |
| Hot reload           | `importlib.reload()` (dev mode)         | Restart container                 |
| Use case             | Hot-path stateless agents               | Everything else                   |

Embedded agents must use `async`/`await` consistently — the SDK
asserts at registration time that the handler is a coroutine
function, and the linter forbids known-blocking imports
(`requests`, sync `sqlite3`, raw `time.sleep`) in embedded modules.

The LLM bridge specifically is **not** an embedded agent in the
rewrite; it's an SDK service (`ctx.llm`). Embedded agents are
reserved for hot-path transformations the SDK doesn't already
provide.

## 7. Testing

The SDK ships a test harness:

```python
from backplaned_sdk.testing import TestRouter

async def test_echo():
    async with TestRouter() as router:
        router.register_embedded(my_agents.echo.agent)
        result = await router.call(
            "echo",
            LLMData(prompt="hello"),
            user_id="test-user",
        )
        assert result.output.content == "HELLO"
```

`TestRouter` runs an in-process router with a sqlite-memory DB, the
local file store, and ACL set to allow-all. `register_embedded`
attaches an agent. `router.call` is a synchronous-feeling wrapper
that drives the full pipeline — frames, ACL, state machine — without
sockets.

For external-agent integration tests, `TestRouter.serve()` exposes a
real WebSocket on a random port; the agent connects normally.

## 8. Worked example: Gemini agent

A realistic agent that uses streaming, tool calls, files, and
provider-specific options. Roughly the shape we'd build the Gemini
suite from.

```python
from backplaned_sdk import Agent, TaskContext, AgentInfo
from backplaned_sdk.types import LLMData, AgentOutput

agent = Agent(
    info=AgentInfo(
        agent_id="gemini_main",
        description=(
            "Gemini-backed conversational agent with web search, "
            "code execution, and image generation."
        ),
        capabilities=[
            "llm.generate.text",
            "llm.generate.image",
            "search.web",
            "exec.code.python",
        ],
        tags=["tier:1", "provider:gemini"],
        accepts=LLMData,
        produces=AgentOutput,
    ),
)

@agent.handler
async def handle(ctx: TaskContext, payload: LLMData) -> AgentOutput:
    ctx.log.info("gemini_main.start")
    ctx.progress.status("thinking")

    response = await ctx.llm.generate(
        prompt=payload.prompt,
        model="gemini-2.5",
        stream=True,
        provider_options={
            "system_instruction": payload.agent_instruction,
            "tools": [
                {"google_search": {}},
                {"code_execution": {}},
            ],
            "thinking_budget_tokens": 4096,
        },
    )

    text_parts: list[str] = []
    files: list[ProxyFile] = []

    async for delta in response:
        if delta.text:
            text_parts.append(delta.text)
        if delta.tool_call and delta.tool_call.name == "image_generation":
            ctx.progress.tool_call("image_generation", delta.tool_call.args)
            image_bytes = await delta.tool_call.await_result()
            pf = await ctx.files.put(
                image_bytes,
                filename="generated.png",
                mime_type="image/png",
            )
            files.append(pf)
            ctx.progress.tool_result("image_generation", {"file_id": pf.path})

    return AgentOutput(content="".join(text_parts), files=files)
```

Notice what the agent does **not** do: no socket code, no `/receive`
endpoint, no manual ack, no token refresh, no progress fan-out
plumbing, no cancellation polling (the SDK handles it inside
`ctx.llm.generate`'s iterator), no provider SDK setup, no API key
handling. The handler reads as business logic.

A coding-tier-2 specialist looks structurally identical, with
different `capabilities`, different `tools`, and different
`provider_options`. That repeatability is the goal of the SDK design.

## 9. Versioning

The SDK is a Python package versioned independently of the router.
Compatibility rules:

- `protocol_version` (in every frame) is bumped on backward-
  incompatible wire changes; routers reject mismatched agents.
- The SDK declares a supported `protocol_version` range; pip
  resolution handles the rest.
- New optional features (e.g. new `Progress` event types) ship as
  minor SDK versions and are no-ops on older routers.
- Breaking SDK API changes (handler signature, `TaskContext` fields)
  bump the SDK major version; agents pin a major.
