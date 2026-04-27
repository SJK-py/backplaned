"""bp_router.acl.rules — Rule grammar models.

See `docs/design/acl.md` §5 for the spec. A `RuleSet` is the parsed
form of `acl.yaml`; deployments load it at startup and reload on
admin updates via PUT /v1/admin/acl/rules.
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Literal, Optional

from pydantic import BaseModel, Field, model_validator

if TYPE_CHECKING:
    from bp_router.settings import Settings


# ---------------------------------------------------------------------------
# Selectors
# ---------------------------------------------------------------------------


class CallerSelector(BaseModel):
    """All conditions are AND-ed; absent fields don't care."""

    tier: Optional[Literal[0, 1, 2]] = None
    tags: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    """Required-capabilities the caller must declare."""
    role: Optional[Literal["admin", "user", "service"]] = None
    user_tier: Optional[str] = None
    self: bool = False
    """Match only when caller and callee are the same agent."""


class CalleeSelector(BaseModel):
    """Mirror of CallerSelector, with provided-capabilities semantics."""

    tier: Optional[Literal[0, 1, 2]] = None
    tags: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    """Provided-capabilities the callee must declare."""
    agent_id: Optional[str] = None
    same_team: bool = False
    """Match when callee shares all `team:*` tags with caller."""


class RuleScope(BaseModel):
    visibility: bool = True
    permission: bool = True


class RuleEffect(str, Enum):
    ALLOW = "allow"
    DENY = "deny"


class Rule(BaseModel):
    name: str
    description: Optional[str] = None
    caller: CallerSelector = Field(default_factory=CallerSelector)
    callee: CalleeSelector = Field(default_factory=CalleeSelector)
    effect: RuleEffect
    scope: RuleScope = Field(default_factory=RuleScope)
    deny_as_not_found: bool = False

    @model_validator(mode="after")
    def _scope_must_be_meaningful(self) -> "Rule":
        if not (self.scope.visibility or self.scope.permission):
            raise ValueError(
                f"rule {self.name!r}: scope.visibility and scope.permission cannot both be False"
            )
        return self


class AclDefaults(BaseModel):
    visibility: RuleEffect = RuleEffect.DENY
    permission: RuleEffect = RuleEffect.DENY


class AclConfig(BaseModel):
    """Parsed `acl.yaml`."""

    defaults: AclDefaults = Field(default_factory=AclDefaults)
    rules: list[Rule] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_acl_config(settings: "Settings") -> AclConfig:
    """Load ACL config from the deployment's `acl.yaml`.

    Resolution order:
      1. `ROUTER_ACL_PATH` env (explicit override).
      2. `./acl.yaml` next to the working directory.
      3. Built-in default (deny-all). Useful for tests.

    Validates the parsed config. If an `acl.tests.yaml` sits next to the
    config, runs it; failed cases marked `required: true` prevent
    startup.
    """
    import logging  # noqa: PLC0415
    import os  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    log = logging.getLogger(__name__)

    candidates: list[Path] = []
    explicit = os.environ.get("ROUTER_ACL_PATH")
    if explicit:
        candidates.append(Path(explicit))
    candidates.append(Path("acl.yaml"))

    chosen: Optional[Path] = None
    for p in candidates:
        if p.is_file():
            chosen = p
            break

    if chosen is None:
        log.info(
            "acl_config_default",
            extra={"event": "acl_config_default", "reason": "no acl.yaml found"},
        )
        return AclConfig()

    try:
        import yaml  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError(
            "PyYAML is required to load acl.yaml — install with `pip install PyYAML`"
        ) from exc

    raw = yaml.safe_load(chosen.read_text(encoding="utf-8")) or {}
    config = AclConfig.model_validate(raw)

    tests_path = chosen.parent / "acl.tests.yaml"
    if tests_path.is_file():
        from bp_router.acl.evaluator import run_acl_tests  # noqa: PLC0415

        test_cases = yaml.safe_load(tests_path.read_text(encoding="utf-8")) or []
        failures = run_acl_tests(config, test_cases)
        required_failures = [f for f in failures if f.get("required", True)]
        if required_failures:
            raise RuntimeError(
                f"ACL test suite failed {len(required_failures)} required case(s): "
                f"{[f['name'] for f in required_failures]}"
            )
        if failures:
            log.warning(
                "acl_tests_optional_failures",
                extra={
                    "event": "acl_tests_optional_failures",
                    "count": len(failures),
                },
            )

    log.info(
        "acl_config_loaded",
        extra={
            "event": "acl_config_loaded",
            "path": str(chosen),
            "rule_count": len(config.rules),
        },
    )
    return config


def load_acl_config_from_dict(data: dict) -> AclConfig:
    """Validate a dict (from PUT /v1/admin/acl/rules) into an AclConfig."""
    return AclConfig.model_validate(data)
