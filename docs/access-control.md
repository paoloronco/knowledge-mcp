# Access control

Two independent controls live in **Settings → Access control** and are all
**disabled by default**. They complement — not replace — the deployment rules in
[Security model](security-model.md).

## 1. MCP authentication

### 1.1 What it does

When enabled, every HTTP request to the MCP endpoint must carry the token. Three
forms are accepted (checked with `hmac.compare_digest`):

```
Authorization: Bearer <token>
X-Api-Key: <token>
POST /mcp?access_token=<token>      # for clients that cannot set headers
```

`GET /healthz` is deliberately **unauthenticated** so liveness probes work.

### 1.2 Enable it

1. Open **Settings → Access control**.
2. Tick *Require an access token on the MCP endpoint*.
3. Leave the token field blank (a random token is generated) or type your own
   (12–1024 characters).
4. Click **Save access settings**. The dashboard restarts MCP with the new env.
5. Copy the displayed token into your MCP client configuration.

The token is stored in `/data/mcp-auth.json`. It is **not** in `config.json` and
is not echoed by `status().config`; it is however part of `/data` and therefore
included in backups — keep archives private.

### 1.3 Scope and limits

- Applies to `streamable-http` and `sse`; `stdio` has no HTTP surface.
- It is a shared static token, not per-user OAuth. For multi-user remote access
  still put an authenticated proxy / [Cloudflare Access](cloudflare-access.md)
  in front, and bind `MCP_HOST` to loopback.
- The endpoint has no rate limiting; treat the token as a secret.

## 2. ACL — allowed roots

### 2.1 What it does

`acl_allow_roots` is an allowlist of absolute document roots. Once non-empty,
a folder can only be selected (and synced) if it sits **under** one of the
listed roots. An empty list keeps the previous, unrestricted behavior.

### 2.2 Where it is enforced

| Layer | Check |
| --- | --- |
| Dashboard | `validate_source_root`, `Controller.update`, `check_folder` |
| Host agent | `resolve_host_folder(root, allow_roots)` before reading the host |
| Folder probe | allowlist attached to `agent_task().folder_probe` |

Example (Settings textarea, one path per line):

```
/mnt/documents
/mnt/shared
```

This closes the gap where a `DAC_READ_SEARCH` companion could otherwise reach
`/etc`, `/root` or `/var/lib/docker` through a dashboard folder selection.

## 3. ACL — denied directories

### 3.1 What it does

`acl_deny_directories` is a list of **directory names** (not paths) that are
excluded at **any depth**, in addition to the mandatory policy exclusions.
Names are case-insensitive.

Example:

```
private
hr
legal
```

### 3.2 Where it is enforced

| Layer | Mechanism |
| --- | --- |
| Ingestion | `is_candidate` and `discover_documents` skip the directory |
| Retrieval | `source_allowed` and `query_filter` add a `must_not` MatchAny on `directories` / `source_top_level` |
| Host agent | `inventory` restricted set + `host_sync.allowed_file` per-file check |
| Dashboard scan | policy overlay in `read_policy()` |

Changing the deny list bumps `sync_revision`, so the host agent re-scans and
documents that became ineligible are pruned on the next incremental update.

## 4. Lexical bigrams

Optional `lexical_bigrams` adds adjacent token pairs to the sparse vector,
improving phrase matching. Qdrant sparse indices are **uint32**, so term IDs
remain 32-bit; bigrams add terms rather than widening the hash.

- Toggling it changes the index signature → **run an incremental update** so
  existing points are rebuilt with the same representation.
- Both ingestion and retrieval read the same `LEXICAL_BIGRAMS` env, so queries
  and stored vectors stay consistent.

## 5. API

`POST /api/access` (dashboard-authenticated, `X-Control-Token` required):

```json
{
  "mcp_auth_enabled": true,
  "mcp_auth_token": "optional-custom-token",
  "acl_allow_roots": ["/mnt/documents"],
  "acl_deny_directories": ["private", "hr"],
  "lexical_bigrams": false
}
```

Response echoes the normalized settings and the active token. Invalid values
return HTTP 400. The four config keys are optional on restore of older backups.

See [Configuration](configuration.md) for the full key/env reference and
[Verification](verification.md) for the tests covering these controls.
