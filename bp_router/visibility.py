"""bp_router.visibility — Build Caller/Callee + available_destinations.

Helpers shared by the WS handshake (Welcome construction) and the task
admit path (ACL check).
"""

from __future__ import annotations

from typing import Any, Optional

from bp_router.acl.evaluator import AclEvaluator, Caller, Callee
from bp_router.db.models import AgentRow


def caller_from_agent(
    agent: AgentRow,
    *,
    role: Optional[str] = None,
    user_tier: Optional[str] = None,
) -> Caller:
    """Build a Caller from an AgentRow plus optional session context."""
    return Caller(
        agent_id=agent.agent_id,
        tags=frozenset(agent.tags),
        capabilities=frozenset(agent.capabilities),
        requires_capabilities=frozenset(agent.requires_capabilities),
        role=role,
        user_tier=user_tier,
    )


def callee_from_agent(agent: AgentRow) -> Callee:
    return Callee(
        agent_id=agent.agent_id,
        tags=frozenset(agent.tags),
        capabilities=frozenset(agent.capabilities),
    )


def available_destinations(
    caller: Caller,
    candidates: list[AgentRow],
    evaluator: AclEvaluator,
) -> dict[str, dict[str, Any]]:
    """Produce the catalog injected into the WelcomeFrame.

    Each entry is a compact view (id + description + tags + capabilities
    + accepts_schema + hidden flag). Agents the caller cannot see are
    omitted entirely — visibility, not just permission.
    """
    result: dict[str, dict[str, Any]] = {}
    for agent in candidates:
        if agent.status != "active":
            continue
        if agent.agent_id == caller.agent_id:
            continue  # don't surface self in catalog
        callee = callee_from_agent(agent)
        if not evaluator.can_see(caller, callee).allow:
            continue
        info = agent.agent_info or {}
        result[agent.agent_id] = {
            "description": info.get("description", ""),
            "capabilities": agent.capabilities,
            "tags": agent.tags,
            "accepts_schema": info.get("accepts_schema"),
            "hidden": info.get("hidden", False),
            "documentation_url": info.get("documentation_url"),
        }
    return result
