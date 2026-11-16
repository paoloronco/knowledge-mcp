# Self-update

The app container intentionally has no Docker socket. Updates are therefore
started with **one command on the Docker host**, not from inside the app. The
updater inspects the running containers, pulls the newest image for the same
tag, and recreates them with an identical configuration. All named volumes and
bind mounts are reused, so settings, the indexing policy, synchronized
documents, the model cache, agent credentials and Qdrant indexes survive.

## One command (recommended)

From a checkout of this repository, on the Docker host:

```bash
git pull
bash scripts/self-update.sh
```

No confirmation prompts and no manual steps. The companion container
(`knowledge-mcp-host-agent`) is updated too when it is present; if it is not
installed, it is skipped automatically.

Without a checkout, the wrapper fetches the updater itself:

```bash
curl -fsSL https://raw.githubusercontent.com/paoloronco/knowledge-mcp/main/scripts/self-update.sh | bash
```

Pass options to a piped command with `bash -s --`, for example
`curl -fsSL …/self-update.sh | bash -s -- --dry-run`.

Or run the Python updater directly with any Python 3:

```bash
python3 scripts/self_update.py --help
```

## What it does

1. Checks that the Docker CLI is reachable.
2. Inspects `knowledge-mcp` and, if present, `knowledge-mcp-host-agent`.
3. Detects a Docker Compose stack and uses `docker compose pull` +
   `docker compose up -d` when Compose files are available.
4. Otherwise reconstructs each `docker run` command from `docker inspect`:
   image tag, environment, volumes, published ports, restart policy, network
   mode, GPU reservations, capabilities, read-only root filesystem, tmpfs
   mounts, entrypoint and command.
5. Pulls the images. If nothing changed, it reports "already up to date" and
   does **not** restart the containers (add `--force` to recreate anyway).
6. Stops and removes the companion first, then the app, then starts the app and
   waits for its Docker health check before starting the companion.
7. If the new app does not become healthy, the previous image IDs are restored
   automatically.

Persistent data is never deleted: only the containers are recreated, never the
volumes. This is the same rule as the manual update procedure, but automated.

## Docker Compose

For a Compose install the updater delegates to Compose automatically. The
equivalent one command is:

```bash
docker compose pull && docker compose up -d
```

For the CUDA image, include both files on every command:

```bash
docker compose -f compose.yaml -f compose.gpu.yaml pull
docker compose -f compose.yaml -f compose.gpu.yaml up -d
```

Never use `docker compose down -v`; that removes the saved data.

## Options

| Option | Purpose |
| --- | --- |
| `--app NAME` | App container name (default `knowledge-mcp`, or `APP_CONTAINER`) |
| `--helper NAME` | Companion container name (default `<app>-host-agent`) |
| `--no-helper` | Do not update the companion |
| `--tag IMAGE` | Pull a different app image tag than the running one |
| `--helper-tag IMAGE` | Pull a different companion image tag |
| `--mode auto\|run\|compose` | Force a strategy instead of auto-detecting |
| `--compose-file FILE` | Compose file to use (repeatable) |
| `--force` | Recreate even when the image digest is unchanged |
| `--dry-run` | Print the exact `docker` commands without changing anything |
| `--stop-timeout N` | Seconds to wait for a graceful stop (default 30) |
| `--timeout N` | Seconds to wait for the app health check (default 240) |

Use `--dry-run` first to review exactly which containers will be recreated:

```bash
bash scripts/self-update.sh --dry-run
```

## Custom deployments

If the containers were created with different names, extra environment
variables, custom ports, a read-only document mount, or `--gpus all`, those
settings are read from the running container and reused automatically. Example:

```bash
bash scripts/self-update.sh --app my-knowledge --helper my-knowledge-agent
```

The updater does not add a Docker socket to the app, does not change the
indexing policy, and does not reindex. After a release that changes chunking or
adds a lexical index, run one incremental update per model index from the
dashboard (see the [README](../README.md#install-from-source--use-the-code)).

## Troubleshooting

- **"Container was not found"**: pass `--app` with the real name, or run from the
  Compose checkout that created the stack.
- **GPU installs**: `--gpus all` is preserved from the running container. Make
  sure the NVIDIA Container Toolkit is still installed on the host.
- **A rollback happened**: the app did not become healthy in time. Check
  `docker logs <app>` and the [troubleshooting guide](troubleshooting.md), fix
  the cause, then run the updater again. Volumes are intact.
- **CUDA image**: the companion always uses `:latest`; only the app uses `:cuda`.
  The updater keeps each container's own tag.
