"""bp_router.acl.evaluator — Rule evaluation algorithm.

First-match wins. Defaults apply when no rule matches. See
`docs/design/acl.md` §6.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from bp_router.acl.rules import (
    AclConfig,
    CalleeSelector,
    CallerSelector,
    Rule,
    RuleEffect,
)
from bp_router.db.models import AgentRow


# ---------------------------------------------------------------------------
# Inputs to evaluation — a Caller carries both the calling agent's static
# attributes (tags, capabilities) and the session's dynamic attributes
# (role, user_tier).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Caller:
    agent_id: str
    tags: frozenset[str]
    capabilities: frozenset[str]
    requires_capabilities: frozenset[str]
    role: Optional[str]
    user_tier: Optional[str]

    @classmethod
    def from_row(
        cls,
        row: AgentRow,
        *,
        role: Optional[str] = None,
        user_tier: Optional[str] = None,
    ) -> "Caller":
        return cls(
            agent_id=row.agent_id,
            tags=frozenset(row.tags),
            capabilities=frozenset(row.capabilities),
            requires_capabilities=frozenset(row.requires_capabilities),
            role=role,
            user_tier=user_tier,
        )


@dataclass(frozen=True)
class Callee:
    agent_id: str
    tags: frozenset[str]
    capabilities: frozenset[str]

    @classmethod
    def from_row(cls, row: AgentRow) -> "Callee":
        return cls(
            agent_id=row.agent_id,
            tags=frozenset(row.tags),
            capabilities=frozenset(row.capabilities),
        )


@dataclass(frozen=True)
class Decision:
    allow: bool
    rule_name: Optional[str]
    """Name of the matched rule, or None if defaults applied."""
    deny_as_not_found: bool = False


DecisionKind = Literal["visibility", "permission"]


# ---------------------------------------------------------------------------
# Evaluator
# ---------------------------------------------------------------------------


class AclEvaluator:
    """Pure-Python evaluator over an `AclConfig`.

    Hot path: called on every `NewTask` admit and every catalog
    construction. Fast: rule list is short, selectors are set
    intersection.
    """

    def __init__(self, config: AclConfig) -> None:
        self._config = config

    def replace(self, config: AclConfig) -> None:
        """Hot-reload after admin update."""
        self._config = config

    # ------------------------------------------------------------------
    # Decisions
    # ------------------------------------------------------------------

    def evaluate(
        self,
        caller: Caller,
        callee: Callee,
        decision: DecisionKind,
        *,
        grants: Optional[list[dict]] = None,
    ) -> Decision:
        """Return the (allow, rule_name) for this pair and decision kind.

        `grants` is the list of scoped grants attached to the current
        task (see `docs/design/acl.md` §7). Grants extend permission
        only — they do not override deny rules and (by default) do not
        affect visibility.
        """
        for rule in self._config.rules:
            if not _scope_matches(rule, decision):
                continue
            if not _caller_matches(rule.caller, caller, callee):
                continue
            if not _callee_matches(rule.callee, callee, caller):
                continue
            return Decision(
                allow=(rule.effect == RuleEffect.ALLOW),
                rule_name=rule.name,
                deny_as_not_found=rule.deny_as_not_found,
            )

        # No rule matched — try grants (permission only)
        if decision == "permission" and grants:
            for grant in grants:
                if _grant_applies(grant, callee):
                    return Decision(allow=True, rule_name="<grant>")

        # Defaults
        default = (
            self._config.defaults.visibility
            if decision == "visibility"
            else self._config.defaults.permission
        )
        return Decision(allow=(default == RuleEffect.ALLOW), rule_name=None)

    # Convenience wrappers
    def can_see(self, caller: Caller, callee: Callee) -> Decision:
        return self.evaluate(caller, callee, "visibility")

    def can_invoke(
        self, caller: Caller, callee: Callee, *, grants: Optional[list[dict]] = None
    ) -> Decision:
        return self.evaluate(caller, callee, "permission", grants=grants)


# ---------------------------------------------------------------------------
# Selector matching helpers
# ---------------------------------------------------------------------------


def _scope_matches(rule: Rule, decision: DecisionKind) -> bool:
    return getattr(rule.scope, decision)


def _caller_matches(sel: CallerSelector, caller: Caller, callee: Callee) -> bool:
    if sel.self and caller.agent_id != callee.agent_id:
        return False
    if sel.role and sel.role != caller.role:
        return False
    if sel.user_tier and sel.user_tier != caller.user_tier:
        return False
    if sel.tier is not None and f"tier:{sel.tier}" not in caller.tags:
        return False
    if sel.tags and not all(t in caller.tags for t in sel.tags):
        return False
    if sel.capabilities and not all(
        c in caller.requires_capabilities for c in sel.capabilities
    ):
        return False
    return True


def _callee_matches(sel: CalleeSelector, callee: Callee, caller: Caller) -> bool:
    if sel.agent_id and sel.agent_id != callee.agent_id:
        return False
    if sel.tier is not None and f"tier:{sel.tier}" not in callee.tags:
        return False
    if sel.tags and not all(t in callee.tags for t in sel.tags):
        return False
    if sel.capabilities and not all(c in callee.capabilities for c in sel.capabilities):
        return False
    if sel.same_team:
        caller_teams = {t for t in caller.tags if t.startswith("team:")}
        callee_teams = {t for t in callee.tags if t.startswith("team:")}
        if not caller_teams or caller_teams != callee_teams:
            return False
    return True


def _grant_applies(grant: dict, callee: Callee) -> bool:
    """Check whether a scoped grant covers the given callee."""
    cid = grant.get("callee_agent_id")
    if cid and cid == callee.agent_id:
        return True
    caps = grant.get("callee_capabilities") or []
    if caps and all(c in callee.capabilities for c in caps):
        return True
    return False
