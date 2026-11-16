# Knowledge MCP — Documentation

Everything about running, configuring and extending the service. Each page is
short and split into numbered sections; follow the index top to bottom.

## 1. Start here

| Page | What you need it for |
| --- | --- |
| [Architecture](architecture.md) | Components, processes and data flow (read first) |
| [Configuration](configuration.md) | Config keys, environment variables, `.env` |
| [Docker deployment](docker.md) | Images, volumes, host mounts, migration |
| [Self-update](self-update.md) | Data-preserving updates |

## 2. Core behavior

| Page | What you need it for |
| --- | --- |
| [Retrieval](retrieval.md) | Hybrid search, tools, limits, migration |
| [Operations](operations.md) | Health, readiness, logs, restarts |
| [Troubleshooting](troubleshooting.md) | Symptom → check → fix |

## 3. Security and access control

| Page | What you need it for |
| --- | --- |
| [Access control](access-control.md) | MCP auth, allowed roots, denied directories |
| [Security model](security-model.md) | Trust boundaries and limitations |
| [Cloudflare Access](cloudflare-access.md) | Protected remote access |

## 4. Reference

| Page | What you need it for |
| --- | --- |
| [Changes](changes.md) | What changed in the latest iteration |
| [Verification](verification.md) | Test commands and coverage |
| [Linux deployment](../knowledge-mcp/mcp/deployment/README.md) | Service example |
| [AI integrations](../AI/README.md) | Remote clients and ADK |

## 5. Conventions

- **Authoritative sources**: the root `README.md` and the pages above.
- **Defaults are safe**: MCP authentication, the ACL and lexical bigrams are all
  **off** until you enable them in Settings.
- **Separate processes**: the dashboard, ingestion and MCP server do not share
  memory; settings reach them through `config.json`, the policy file and
  environment variables (see [Configuration](configuration.md#4-how-settings-travel)).
