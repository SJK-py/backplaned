"""bp_router.llm — Centralised LLM service.

Promoted from "embedded agent" to a router-side service so:
- Provider API keys never leave the router process.
- Quotas and budgets are enforced consistently across providers.
- Telemetry (tokens, cost, latency) is uniform.

See `docs/design/sdk/services.md` §1 for the agent-facing contract.
"""

from bp_router.llm.service import (
    LlmDelta,
    LlmResponse,
    LlmService,
    Message,
    TokenUsage,
    ToolCall,
    ToolChoice,
    ToolSpec,
)

__all__ = [
    "LlmDelta",
    "LlmResponse",
    "LlmService",
    "Message",
    "TokenUsage",
    "ToolCall",
    "ToolChoice",
    "ToolSpec",
]
