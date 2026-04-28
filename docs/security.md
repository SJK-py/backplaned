# Security — Threat Model, Tokens, Secrets

> Security model for the reworked Backplaned. Prescribes the threat
> model, trust boundaries, authentication/authorization, token
> lifecycle, secrets handling, and audit trail. Companion to
> [`acl.md`](./acl.md) (which covers permission rules) and
> [`observability.md`](./observability.md) (which covers audit
> logging mechanics).

## 1. Threat model

### 1.1 Assets

| Asset                           | Why it matters                                       |
| ------------------------------- | ---------------------------------------------------- |
| User content (prompts, files)   | Privacy; potential PII; possible regulated data      |
| Provider API keys               | Direct cost impact; abuse risk                       |
| Agent auth tokens               | Lateral movement into the agent network              |
| Admin credentials               | Full control over users, agents, ACL                 |
| Audit log integrity             | Regulatory and forensic requirement                  |
| Quota / billing counters        | Financial integrity                                  |

### 1.2 Adversaries

- **External attacker** (no credentials). Goals: exfiltrate data,
  consume budget, disrupt service, pivot to provider keys.
- **Compromised user account**. Goals: exfiltrate other users'
  data, escalate privileges, exhaust shared budget.
- **Compromised agent process**. Goals: as above, plus impersonate
  legitimate routing, extract API keys.
- **Malicious agent author**. A registered agent intentionally
  abusing its capabilities. Goals: exfiltrate data passing through
  it, escalate via crafted frames.
- **Insider with admin role**. Out of scope for technical controls
  beyond audit logging.

### 1.3 Out of scope

- Kernel- and hypervisor-level attacks.
- Side-channel attacks against in-process embedded agents (sharing
  a process is a stated trust assumption — see §2).
- Coercion of authorised users.

## 2. Trust boundaries

```
   ┌─────────────────────────────── public network ───────────────────────────────┐
   │                                                                              │
   │  ┌────────────┐    HTTPS    ┌───────────┐                                    │
   │  │ User UA    │ ──────────► │  Webapp   │                                    │
   │  └────────────┘             │  (BFF)    │                                    │
   │                             └─────┬─────┘                                    │
   │                                   │  HTTPS / WSS (session JWT)               │
   │                                   ▼                                          │
   │  ┌──────────────────────────────────────────────────────────────────────┐   │
   │  │                              Router                                  │   │
   │  │  trust ⟦ embedded agents ⟧ same-process; isolated only by SDK rules  │   │
   │  └──────────────────────────────────────────────────────────────────────┘   │
   │           ▲                                  ▲                              │
   │           │ WSS (agent JWT)                  │ HTTPS (admin)                │
   │           │                                  │                              │
   │  ┌────────┴─────────┐                ┌───────┴───────┐                      │
   │  │ External agents  │                │   Admin UA    │                      │
   │  └──────────────────┘                └───────────────┘                      │
   │                                                                              │
   └──────────────────────────────────────────────────────────────────────────────┘
```

Boundaries (each is a place where authentication is required):

1. User browser → Webapp BFF.
2. Webapp / Admin → Router HTTP API.
3. External agent → Router WebSocket.
4. Router → Storage backend (S3 / Postgres / Redis).
5. Router → LLM provider APIs.

Embedded agents are inside the router's trust boundary. This is a
deliberate trade for hot-path latency (see overview §P8) and the
reason the SDK forbids known-blocking imports and provider-secret
direct access from embedded handlers.

## 3. Authentication

### 3.1 Users

- **Password + TOTP** for human users (preferred). Admin role
  requires TOTP unconditionally.
- **OIDC** (Google, Microsoft, GitHub) supported via standard
  flows; the router persists only `sub` + email + role.
- **Service principals** authenticate with a long-lived API key
  (rotatable) carried as `Authorization: Bearer <key>`.

Passwords are stored using `argon2id` (replacing the current
PBKDF2 in `helper.py:34-52` — PBKDF2 is acceptable for legacy but
argon2id is the default for greenfield). Hash parameters are
config-tunable; defaults follow OWASP 2024 recommendations.

After successful login, the router issues a short-lived **session
JWT** (default 15 min) and a refresh token (default 24 h, single-
use, rotation on refresh).

### 3.2 Agents

External agents authenticate to the router with an **agent JWT** —
short-lived (default 24 h), rotated automatically by the SDK. JWTs
are signed with a router-side secret (HS256) or asymmetric key
(EdDSA) — asymmetric is recommended for multi-worker deployments so
verification scales without sharing the signing secret.

JWT claims:

```jsonc
{
  "iss": "router",
  "sub": "<agent_id>",
  "iat": 1234567890,
  "exp": 1234654290,
  "kind": "agent",
  "ver": 1,                      // protocol version supported
  "jti": "<uuid>"                // for revocation
}
```

The agent JWT is presented in the `Hello` frame's `auth_token`
field. The router validates signature, expiry, claims, and
revocation list. A revoked `jti` causes immediate socket close.

### 3.3 Optional agent identity (asymmetric)

For higher-assurance deployments, agents may register with an
ed25519 public key. On `Hello`, the agent signs a server-issued
challenge with its private key. This raises the bar from
"compromised JWT" to "compromised host key."

Agent identity registration happens at onboarding (POST
`/v1/onboard` carries the public key). Loss of the key requires
admin re-issuance; the router does not support self-service key
rotation for now.

## 4. Authorization

Three layers, all enforced server-side, in this order:

1. **Authentication.** Reject unauthenticated requests at the edge.
2. **Role / tier check.** Admin endpoints require `admin`. User-
   facing endpoints require `user` or `service`.
3. **ACL check.** For agent-to-agent calls, evaluate the
   capability/tag rules ([`acl.md`](./acl.md)).

User-data access (own files, own tasks, own sessions) is enforced
by foreign-key scoping in every query — the data layer never
returns rows from another user's `user_id` regardless of the
caller. This is a `WHERE user_id = $current_user_id` invariant
checked by a single helper used by every read path.

## 5. Token lifecycle

### 5.1 Issuance

- **Session JWT.** Issued on login or refresh. Carries `user_id`,
  `role`, `user_tier`, `iat`, `exp`. Signed by router secret.
- **Refresh token.** Issued alongside the session JWT. Stored
  hashed in `auth_refresh_tokens(token_hash, user_id, expires_at,
  used_at, replaced_by)`. Single-use; on refresh, the old row is
  marked `used_at` and a new pair is issued.
- **Agent JWT.** Issued at onboarding and at every refresh
  (`POST /v1/agent/refresh-token`). Refresh requires the agent to
  present its current valid JWT plus its registered identity
  (asymmetric mode) or its long-term shared secret.

### 5.2 Revocation

- A `jti` revocation list is held in Redis with TTL = JWT
  remaining lifetime. Cheap to check on every frame admit.
- Admin can revoke an agent (`POST /v1/admin/agents/{id}/suspend`)
  or a user session via the same mechanism.
- Mass revocation (e.g. signing key rotation) is supported by
  bumping a global `key_version` and rejecting JWTs signed against
  earlier versions.

### 5.3 Refresh-token theft mitigation

Single-use refresh tokens with rotation detect token replay: if a
refresh token's `used_at` is set when presented, the entire token
family is invalidated (forces re-login) and an audit event is
emitted. This is the standard OAuth2 refresh-token rotation
pattern.

## 6. Secrets management

### 6.1 Categories

| Secret                     | Where it lives                                | Who reads it                  |
| -------------------------- | --------------------------------------------- | ----------------------------- |
| JWT signing key            | Env / KMS / HSM                               | Router only                   |
| Provider API keys          | Secrets backend (Vault / AWS SM / GCP SM)     | LLM service (router) only     |
| Database password          | Env / secrets backend                         | Router only                   |
| Redis password             | Same                                          | Router only                   |
| Storage credentials        | Same                                          | Router only                   |
| Per-user provider keys     | DB (encrypted at rest with envelope keys)     | LLM service per request       |
| Agent shared secret / pubkey | DB                                          | Router only                   |
| User password hashes       | DB                                            | Router only (verify)          |

### 6.2 Provider API keys

Critical: provider API keys do not live in agent processes. The LLM
bridge is an SDK service backed by the router; the router holds keys
and enforces quotas. Embedded agents that need provider access call
`ctx.llm.generate(...)`, which fans out to the router-side service.

If user-supplied (BYO-key) flows are required, keys are stored
encrypted at rest using envelope encryption (KMS-issued data key
per user), decrypted in memory at use time, and never logged.

### 6.3 Loading secrets

Configuration prefers references over inline values:

```toml
[router]
jwt_secret = { secret_ref = "vault://kv/router/jwt_secret" }
db_url     = { secret_ref = "env://DATABASE_URL" }
```

The Pydantic Settings layer resolves `secret_ref` at startup. A
deployment that hasn't configured a secrets backend can use plain
env vars; production deployments should not.

### 6.4 Rotation

- JWT signing key: rotate via dual-version overlap. New JWTs are
  signed with `key_version=N+1` while the router still verifies
  `key_version=N` for the JWT lifetime.
- Provider API keys: rotated by updating the secrets backend; the
  router refetches on the next request (or on a scheduled
  interval).
- DB / Redis / storage credentials: standard infra rotation;
  router supports config reload via SIGHUP without dropping
  connections.

## 7. Network security

- TLS 1.2+ on every external interface; modern cipher suites only.
  Self-signed certificates rejected in production via a startup
  check.
- WebSocket connections require WSS in production (HTTP plain
  rejected at the edge).
- Network policies (deployment-local): router accepts inbound only
  from the load balancer subnet; outbound to provider endpoints
  is allowlisted; Redis and Postgres are private-subnet only.
- Storage backends (S3 / GCS) accessed via VPC endpoints where
  available; presigned URLs scoped to single objects with short
  TTL.

## 8. Data isolation

### 8.1 Per-user

- `WHERE user_id = ?` invariant on every read of `tasks`,
  `sessions`, `files`, `audit_log` (when scoped). Enforced by a
  single query helper; CI greps for raw queries that bypass it.
- Postgres deployments may additionally enable Row-Level Security
  (RLS) policies as defence-in-depth.

### 8.2 Per-session

- `session_id` foreign keys enable session-scoped memory and file
  visibility. Cross-session reads are explicit (the orchestrator
  may copy state forward at session-open time).

### 8.3 Inter-agent

- Frames passing through the router carry `user_id` and
  `session_id`; the router validates that a frame's claimed
  `user_id` matches the calling agent's session. Attempts to
  forge `user_id` cross-session are rejected with `acl_denied`
  and logged as a security event.

## 9. Audit log

`audit_log` is append-only, hash-chained. Each entry references the
hash of the previous entry (Merkle-style); the head is periodically
checkpointed externally for tamper-evidence.

Recorded events:

```
user.created          user.suspended      user.role_changed
session.opened        session.closed
agent.onboarded       agent.suspended     agent.token_refreshed
acl.rules_replaced    acl.grant_issued    acl.deny
auth.login_succeeded  auth.login_failed   auth.refresh_replayed
quota.exceeded
secret.accessed       secret.rotated
admin.action          (catch-all for admin endpoint hits)
```

Audit reads require `admin` role. The audit endpoint supports
filtering and time-range queries but not deletion. Operational
cleanup (compaction, archival) is performed via a separate
backend tool, not the API.

## 10. Specific risks and mitigations

| Risk                                           | Mitigation                                                 |
| ---------------------------------------------- | ---------------------------------------------------------- |
| Compromised agent JWT replays old frames       | Frame timestamps within ±60s window; replay rejected.      |
| Compromised user logs in from a new IP         | Session JWT rebinding to IP optional; alert on change.     |
| Slow / malicious agent monopolises a socket    | Per-socket outbox bound; per-frame ack timeout; reaper.    |
| Agent fakes `user_id` in a `NewTask`           | Router cross-validates against socket's session.           |
| Embedded agent does sync I/O                   | SDK lints handler at registration; ASYNC_ONLY flag.        |
| Provider API key leaks via prompt injection    | Keys never in agent processes; injection cannot exfiltrate.|
| Refresh token theft                            | Single-use refresh + family invalidation on replay.        |
| Audit log tampering                            | Hash-chained, externally checkpointed.                     |
| Quota counter race in multi-worker             | Postgres advisory locks or atomic UPDATE ... RETURNING.    |
| Onboarding token reuse                         | Single-use; `used_at` on consumption; rejected after.      |
| Cross-tenant file access via path traversal    | Content-addressed (sha256) storage; paths are computed.    |
| Large frame DOS                                | `max_payload_bytes` enforced; oversized closes the socket. |
| Long-running CPU loop ignores cancel           | Hard deadline timeout; SDK `raise_if_cancelled` in helpers.|

## 11. Operational hygiene

- **Backups.** Postgres + storage backend daily. Audit log
  separately, retained ≥1 year.
- **Pen tests.** Annually, with focus on auth flows, ACL bypass,
  and frame injection.
- **Dependency scanning.** SBOM generated per release; CVEs fail
  CI at HIGH+.
- **Secret leak scanning.** Pre-commit + CI, against the full
  repo and recent commits.
- **Incident response.** A documented runbook for: token
  compromise, agent compromise, signing-key rotation, mass
  revocation.

## 12. What this design does **not** protect against

- Operator-level compromise of the router host (host gets you
  everything: signing keys, provider keys, all data).
- Malicious code shipped inside an embedded agent module — the
  embedded trust boundary is the router process.
- Coercion / social engineering of admins.
- Provider-side breaches (provider holding cleartext prompts).

These are accepted risks; mitigation is operational, not
architectural.
