# ACL — Capabilities, Tiers, Scoped Grants

> Deep dive on the access-control model. Companion to
> [`router/state.md §3`](./router/state.md#3-capability-based-acl), which
> introduced the primitives. This document specifies the rule grammar,
> evaluation algorithm, and corner cases.

## 1. What this replaces

The current Backplaned ACL is two columns on the agents table —
`inbound_groups` and `outbound_groups` (`router.py:401, 443`). To let
agent A call agent B, an admin edits both rows. The mechanism works
but does not scale:

- N² maintenance: every new agent requires updating multiple groups.
- No expression of _why_ A may call B.
- No path to per-user gating, capability discovery, or auditable
  one-shot extensions.

The rewrite keeps the same goal — narrow visibility, controlled
permission — but expresses it as **capability-based rules** computed
from declarations rather than hand-edited tables.

## 2. Vocabulary

| Term         | Meaning                                                            |
| ------------ | ------------------------------------------------------------------ |
| Capability   | Dotted-namespace string for a function (e.g. `llm.generate.text`). |
| Tag          | Free-form label on an agent (`tier:1`, `team:coding`).             |
| Tier         | Conventional integer 0/1/2 carried as a tag.                       |
| Role         | User-side permission set: `admin`, `user`, `service`.              |
| Provides     | Capabilities an agent declares it fulfils.                         |
| Requires     | Capabilities an agent's handlers may invoke.                       |
| Visibility   | Which agents appear in a caller's `available_destinations`.        |
| Permission   | Which agents a caller may actually invoke.                         |
| Scoped grant | One-shot ACL extension attached to a task or task tree.            |

## 3. Capability namespace

Capabilities are dotted strings, lowercase, ASCII. The first segment
identifies a domain. Reserved domains:

| Domain     | Examples                                                                |
| ---------- | ----------------------------------------------------------------------- |
| `llm`      | `llm.generate.text`, `llm.generate.image`, `llm.generate.video`         |
| `search`   | `search.web`, `search.knowledgebase`                                    |
| `exec`     | `exec.code.python`, `exec.shell`                                        |
| `files`    | `files.read.user`, `files.write.user`, `files.read.shared`              |
| `memory`   | `memory.read.session`, `memory.write.session`, `memory.read.user`       |
| `notify`   | `notify.email`, `notify.webhook`                                        |
| `admin`    | `admin.users.write`, `admin.acl.write`, `admin.audit.read`              |
| `internal` | Reserved for SDK / router; not callable by user agents.                 |

Custom domains for deployment-specific capabilities are permitted
but should be prefixed with the deployment owner (e.g.
`acmecorp.crm.lookup`). The router rejects capability strings that
don't match `^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$`.

Granularity rule of thumb: declare what a caller would reasonably
gate on. `llm.generate` is too coarse (image generation is a
different cost centre); `llm.generate.text.gemini.2.5.pro` is too
fine (model picks shouldn't be ACL choices). The `domain.action.scope`
shape is the sweet spot.

## 4. Tags

Tags are `key:value` strings (or bare strings, treated as
`tag:<value>`). Examples:

```
tier:0           tier:1           tier:2
team:coding      team:research    team:ops
provider:gemini  provider:openai  provider:anthropic
region:us        region:eu
trust:high       trust:experimental
```

Tags are unstructured by design. The ACL rule grammar matches on tag
sets, never on the meaning of a tag. Conventions are deployment-
local — establish them in your deployment's `acl.yaml` and stick to
them.

A reserved tag namespace `_router:*` is used internally (e.g.
`_router:embedded` to mark embedded agents) and cannot be set by
agents themselves.

## 5. Rule grammar

ACL config lives in `acl.yaml` and is loaded into the `acl_rules`
table on startup or via `PUT /v1/admin/acl/rules`.

```yaml
defaults:
  visibility: deny
  permission: deny

rules:
  - name: <string>                    # required, unique
    description: <string>             # optional
    caller:                           # all conditions must match (AND)
      tier: 0|1|2                     # shorthand for tags: [tier:N]
      tags: [<string>, ...]           # all tags must be present
      capabilities: [<string>, ...]   # caller's required-capabilities (AND)
      role: admin|user|service        # session role
      user_tier: free|paid|enterprise
    callee:                           # all conditions must match (AND)
      tier: 0|1|2
      tags: [<string>, ...]
      capabilities: [<string>, ...]   # callee's provided-capabilities (AND)
      agent_id: <string>              # exact match (rarely used)
    effect: allow|deny
    scope:                            # which decisions this rule governs
      visibility: true                # default true
      permission: true                # default true
```

Selectors that are absent match anything ("don't care"). Rules with
both `visibility: false` and `permission: false` are nonsensical and
rejected at load time.

### 5.1 Special selectors

- `caller: { self: true }` — only the agent itself satisfies this.
  Rare; useful for "an agent may always call its own helper subagent."
- `callee: { same_team: true }` — matches when callee shares all
  `team:*` tags with caller. Convenient for tier-2 peer rules.

## 6. Evaluation algorithm

Every visibility check (catalog construction, `find_agents`,
`describe_agent`) and every permission check (`NewTask` admit) runs
the same procedure:

```
def evaluate(caller, callee, decision: "visibility"|"permission") -> bool:
    for rule in rules_in_order:
        if decision == "visibility" and not rule.scope.visibility: continue
        if decision == "permission" and not rule.scope.permission: continue
        if not match(rule.caller, caller): continue
        if not match(rule.callee, callee): continue
        return rule.effect == "allow"
    return defaults[decision] == "allow"   # i.e. False given deny defaults
```

First-match wins. Order matters; rules are stored with an `ord`
column. Admin endpoints validate the ruleset against a curated
test suite (see §10) before persisting.

## 7. Scoped grants

A scoped grant is an ACL extension attached to a task. It is the
escape hatch for the strict-tier failure mode (tier-1 needs a tier-2
specialist not in its visibility set).

```jsonc
{
  "type": "NewTask",
  "...": "...",
  "acl_grants": [
    {
      "callee_agent_id": "test_writer",
      "expires": "for_task_tree"          // for_task | for_task_tree | for_session
    },
    {
      "callee_capabilities": ["search.web"],
      "expires": "for_task"
    }
  ]
}
```

### 7.1 Grant rules

- **Granter must hold what it grants.** A caller cannot grant
  permission it does not itself have. The router validates at
  `NewTask` admit time and rejects with `Error{code:"acl_grant_invalid"}`.
- **Grants apply only to permission, not visibility-by-default.**
  Granted callees become reachable (callable) but do not
  automatically appear in catalogs unless the recipient agent re-
  fetches via `ctx.peers.find()`.
- **Grants are auditable.** Every grant generates a `task_events`
  entry recording granter, grantee, scope, and expiry.
- **Grants chain.** When `expires=for_task_tree`, the grant
  propagates to descendants spawned by the recipient. Each
  propagation step is logged.
- **Grants do not override deny.** A `deny` rule in the static
  ruleset still wins. Grants extend permission only where the
  static ruleset is silent (defaults apply).

### 7.2 When to use a grant vs. edit the ruleset

Use a grant for **one-off, contextual** access where the static
rule would be over-broad. Edit the ruleset when the access pattern
is recurring — recurring grants are a code smell pointing at a
missing rule.

## 8. Discovery and tool catalog

`available_destinations`, surfaced in the `Welcome` frame and
re-fetchable via `ctx.peers.visible()`, is the visibility-filtered
catalog. Construction:

1. Enumerate all registered agents.
2. For each, run `evaluate(caller, callee, "visibility")`.
3. Keep visible agents. For each, attach a compact entry:
   `{agent_id, description, tags, capabilities, has_documentation}`.
4. Inject as `available_destinations` and (in the SDK) translate to
   provider tool schemas via `build_tools()`.

`describe_agent(id)` performs the same visibility check before
returning the full `AgentInfo`. Permission-only callees (visible
through a scoped grant but not via static rules) are omitted from
the catalog by default; pass `include_granted=True` to surface them.

`find_agents(capability)` returns ranked matches whose `provides`
list contains the capability and which pass visibility. Ranking is
deployment-defined — typical signal sources are recent success rate,
average latency, and explicit `priority` tags.

## 9. Per-user composition

Two ACL layers compose:

- **User → agents:** which agents this user's session may reach
  (driven by `role`, `user_tier`).
- **Agent → agents:** which agents an in-flight handler may invoke
  (driven by tags, capabilities, scoped grants).

The effective permission is the intersection. A premium-tier agent
called by a free-tier user's session is denied even if the orchestrator
has permission. This intersection is enforced at the router on every
`NewTask` admit; it is not advisory.

Implementation: the user's session JWT carries `role`, `user_tier`,
and resolved permission tags. The router merges these into the
caller selector before running `evaluate()`. The agent never sees
raw user identity for ACL purposes — only the resolved tags.

## 10. Testing rulesets

ACL configuration is too easy to get wrong silently. The rewrite
ships a deployment-local test format:

```yaml
# acl.tests.yaml
- name: orchestrator-can-reach-gemini
  caller: { agent_id: orchestrator }
  callee: { agent_id: gemini_main }
  expect: { visibility: allow, permission: allow }

- name: free-tier-cannot-image-gen
  caller: { agent_id: orchestrator, user_tier: free }
  callee: { agent_id: gemini_image }
  expect: { permission: deny }

- name: tier2-peers-within-team
  caller: { agent_id: code_writer }
  callee: { agent_id: test_writer }
  expect: { visibility: allow, permission: allow }
```

`backplaned-acl-test acl.yaml acl.tests.yaml` runs the cases
offline. `PUT /v1/admin/acl/rules` rejects updates that fail any
test case marked `required: true`. CI runs the suite on every
ruleset change.

## 11. Observability for ACL

Every decision emits a structured log line and a metric counter:

```
router_acl_decisions_total{
  decision="visibility|permission",
  effect="allow|deny",
  rule_name="...",
  caller_tier="0|1|2|none",
  callee_tier="0|1|2|none"
}
```

A persistent deny rate from a specific caller against a specific
callee is the strongest signal that the ruleset has a gap.
Recommended dashboards (see [`observability.md`](./observability.md)):

- Top 10 (caller, callee) pairs by deny count.
- Empty-result rate from `find_agents(capability)`.
- Rate of scoped-grant usage by capability — frequent grants point
  to a missing static rule.

## 12. Migration from group-based ACL

For a deployment cutting over from the legacy router:

1. Map each existing group to a tag (`group:X` → `team:X` or
   `tier:N`).
2. Generate a baseline ruleset from existing pairs:
   `caller: { tags: [group:X] }, callee: { tags: [group:Y] }, allow`.
3. Run the test suite to ensure no behaviour gap.
4. Replace pair-based rules with capability-based rules
   incrementally; test cases pin the behaviour while the rules
   evolve.
5. Once the ruleset is capability-driven, retire the bridge tags.

Only step 1 is mechanical; the rest is judgement. The migration is
worth doing — the new model pays back as soon as a third agent in a
"group" appears.

## 13. Pitfalls

- **Rule order drift.** Inserting a new rule near the top reorders
  evaluation. Always run the test suite after edits.
- **Over-broad capability declarations.** An agent that declares
  `provides: [llm.generate.text]` claims to be a general LLM
  bridge — callers will route to it. Declare narrowly.
- **Implicit deny is not a feature.** Adding a deny rule on top of
  default-deny is redundant. Use deny rules only to override an
  earlier allow.
- **Granting capabilities you don't have.** The router rejects
  this, but agent code that builds grants from user input must
  pre-validate or surface the error gracefully.
- **Visibility leak via error messages.** A caller that gets
  `acl_denied` for a callee learns the callee exists. For
  high-confidentiality cases, return `not_found` instead of
  `acl_denied`. The router supports a per-rule
  `deny_as_not_found: true` flag for this.
