# backplaned

Multi-user agent router with a WebSocket transport, a Postgres backend,
and a typed Python SDK.

A central router multiplexes typed frames between agents over a single
WebSocket per agent, persists task state in Postgres with an enforced
state machine, gates inter-agent calls through a capability-based ACL,
and exposes provider-tailored LLM bridges as a first-class service.

The full design lives in [`docs/`](./docs):

- [`docs/overview.md`](./docs/overview.md) — principles, architecture, departures from the legacy stack.
- [`docs/router/`](./docs/router) — wire protocol, task state machine, schema, HTTP API, sequencing.
- [`docs/sdk/`](./docs/sdk) — agent surface, transports, services, worked Gemini agent example.
- [`docs/acl.md`](./docs/acl.md) — capabilities, tiers, scoped grants.
- [`docs/observability.md`](./docs/observability.md) — span/log/metric conventions.
- [`docs/security.md`](./docs/security.md) — threat model, tokens, secrets.

## Packages

```
bp_protocol/   # Shared frames + types (consumed by both router and SDK)
bp_router/     # The router — FastAPI + asyncpg + Redis + WebSockets
bp_sdk/        # The agent SDK — Agent, TaskContext, services
```

## Install

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[router,sdk,llm-gemini,dev]"
```

Optional extras:

| Extra            | Adds                                    |
| ---------------- | --------------------------------------- |
| `router`         | Router runtime (Postgres, Redis, etc.)  |
| `llm-gemini`     | `google-genai` for Gemini provider      |
| `storage-s3`     | `aioboto3` for S3/R2/MinIO              |
| `dev`            | `pytest`, `ruff`, `mypy`                |

The agent SDK only needs the core dependencies (no optional extras
required) to run external agents.

## Run the router (local)

```bash
# 1. Start Postgres.
docker compose -f docker-compose.dev.yml up -d

# 2. Apply the schema.
export ROUTER_DB_URL=postgresql://postgres:bp@localhost:5432/bp_router
alembic upgrade head

# 3. Configure and run the router.
export ROUTER_PUBLIC_URL=http://localhost:8000
export ROUTER_JWT_SECRET=$(openssl rand -base64 32)
bp-router
```

The router listens on port 8000. Health endpoints are at `/healthz`
and `/readyz`; Prometheus exposition at `/metrics`; OpenAPI docs at
`/docs`.

## Write an agent

```python
from bp_protocol.types import AgentInfo, AgentOutput, LLMData
from bp_sdk import Agent, TaskContext

agent = Agent(info=AgentInfo(
    agent_id="echo",
    description="Echoes the prompt back, in uppercase.",
    capabilities=["text.transform.uppercase"],
))

@agent.handler
async def handle(ctx: TaskContext, payload: LLMData) -> AgentOutput:
    return AgentOutput(content=payload.prompt.upper())

if __name__ == "__main__":
    agent.run()
```

To run, the agent needs an invitation token from a router admin:

```bash
export AGENT_ROUTER_URL=ws://localhost:8000/v1/agent
export AGENT_INVITATION_TOKEN=...   # one-shot from POST /v1/admin/invitations
python my_agent.py
```

The SDK persists credentials under `state_dir/credentials.json` after
the first onboarding so subsequent runs reconnect with the cached
auth token.

## Tests

```bash
# End-to-end smoke test against a real router + Postgres
export TEST_DB_URL=postgresql://postgres:bp@localhost:5432/bp_router
alembic upgrade head
pytest tests/test_smoke_e2e.py -xvs
```

## Status

Early-development. The wire protocol, frame schema, DB schema, ACL
grammar, JWT lifecycle, and SDK surface are stable. Items still on the
roadmap include non-Gemini LLM provider adapters
(Anthropic/OpenAI), HashiCorp Vault / AWS Secrets Manager / GCP
Secret Manager backends for `secret_ref://`, and finer-grained OTel
span instrumentation across the dispatch path.

## License

See [LICENSE](./LICENSE).
