"""bp_router.acl — Capability-based access control.

See `docs/acl.md` for the full specification.
"""

from bp_router.acl.evaluator import AclEvaluator, Decision
from bp_router.acl.rules import (
    AclConfig,
    CalleeSelector,
    CallerSelector,
    Rule,
    RuleEffect,
    RuleScope,
    load_acl_config,
)

__all__ = [
    "AclConfig",
    "AclEvaluator",
    "CalleeSelector",
    "CallerSelector",
    "Decision",
    "Rule",
    "RuleEffect",
    "RuleScope",
    "load_acl_config",
]
