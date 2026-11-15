# Docker deployment notes

The image starts the Web UI and bundled Qdrant. Docker must configure port publication, persistent storage, and host folder access when creating containers. The [root README](../README.md#docker-image) gives complete `docker run` commands for the app and a separate Linux host agent. A bare `docker run paoloronco/knowledge-mcp` does not supply those settings.

## Linux/NAS deployment

Alternatively, the repository's [compose.yaml](../compose.yaml) configures the app and automatic host agent together, publishes ports 8080 and 8000, and retains data in Docker volumes. From a local repository checkout:

Compose creates two distinct containers from `paoloronco/knowledge-mcp:latest`: `knowledge-mcp` for the Web UI, MCP, and Qdrant, and `knowledge-mcp-host-agent` for `host_agent.py`. The host agent mounts the Linux host at `/host` read-only and shares the app container's network.

For an NVIDIA GPU, install the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html) on the Docker host, then run `docker compose -f compose.yaml -f compose.gpu.yaml up -d`. The override changes only the app container to `paoloronco/knowledge-mcp:cuda` and reserves one NVIDIA GPU; the host agent remains on `:latest`. Use both `-f` flags on subsequent `pull`, `up`, and `stop` commands. The CUDA image uses PyTorch CUDA 12.6 when available and falls back to CPU otherwise. The dashboard displays the driver, GPU memory, and actual CUDA readiness. The [GPU Compose reservation](https://docs.docker.com/compose/how-tos/gpu-support/) requires Docker GPU support.

```bash
docker compose up -d
```

The app stores Qdrant data at `/qdrant/storage`. Settings, ingestion state, the model cache, and synchronized documents are under `/data`. A third volume retains the agent connection. To update, run `docker compose pull` followed by `docker compose up -d`. To stop the services, run `docker compose stop`.

For the README's `docker run` installation, update by stopping and removing the host agent first, then the app container; pull the new image and repeat both README commands. Keep the three named volumes. Recreate the agent whenever you recreate the app container because it joins the app container's network namespace.

Keep the same Compose project name and volume mappings when updating. Do not use `docker compose down -v` or delete/prune the application's volumes: that removes the saved data.

## Downloading and importing a backup

Open **Settings → Backup and restore** in the Web UI. The setup screen also links to Settings on a new instance. Choose **Download backup**, then import the `.tar.gz` archive on another installation of the same app version with bundled Qdrant and the same automatic host agent configuration. Wait for indexing, scans and document synchronization to finish first. Qdrant and MCP stop while creating the archive and resume afterwards. The dashboard stays responsive during that pause: status reads keep working and other changes return `503` with a maintenance message instead of blocking; `/api/health` stays healthy so Docker does not mark the container unhealthy. The browser downloads directly to disk; the temporary download link expires after ten minutes and can be used once. Creating another backup discards the previous unused download. Restore checks the complete gzip checksum, volume layout, Qdrant version, application settings, credentials and indexing policy before replacing data; an installation error rolls back the file moves. The web process then restarts inside the same container, keeping the host agent's shared network connected. Reload the page and use the dashboard password saved in the backup.

The archive contains `/data` (settings, policy, ingestion state, synchronized documents and login records), `/qdrant/storage` (all indexed models' collections), and the configured automatic host agent directory. It omits the downloaded model cache; models download again when needed, without rebuilding the saved vectors. External read-only document mounts, source folders on the host, Docker port/GPU settings and custom environment variables are not copied. Recreate those mounts/settings and ensure the configured host folder paths exist before enabling synchronization or indexing on the destination. Updating within one installation only needs the existing volumes; importing a backup is for migration or recovery.

Backups contain private documents and agent credentials and are not encrypted. Store them privately and import only trusted archives. Uploads and extracted contents are limited to 100 GiB and 250,000 archive entries; export enforces the same file count and extracted size limits. Allow space for the compressed archive in the container's temporary directory and extracted data alongside the current volumes. Qdrant storage backups require the exact Qdrant version recorded in the archive; restore into the same app version first, then update normally. Unsupported backup formats are rejected. An interrupted restore (host crash/power loss) is not transactional across the three Docker volumes; retain the original archive for recovery.

From a Linux repository checkout, verify backup/restore with the image's real Qdrant binary in an isolated container (no application volumes or host documents mounted):

```bash
docker run --rm --entrypoint python -v "$PWD:/tests:ro" \
  paoloronco/knowledge-mcp:latest \
  -m unittest discover -s /tests/tests -p test_backup.py
```

This check creates test vectors in two collections, exports them, removes one collection, restores the archive and queries both collections again. Without a Qdrant binary, ordinary local Python tests skip this integration check; set `QDRANT_TEST_BINARY` to an installed binary to run it locally.

## Switching an existing `docker run` installation to Compose

The README's `docker run` commands use volumes named `knowledge_app`, `knowledge_qdrant`, and `knowledge_agent`. To reuse them in Compose, create `compose.override.yaml` beside `compose.yaml`:

```yaml
volumes:
  app_data:
    external: true
    name: knowledge_app
  qdrant_data:
    external: true
    name: knowledge_qdrant
  host_agent_data:
    external: true
    name: knowledge_agent
```

Stop and remove the old containers, then start Compose:

```bash
docker stop -t 30 knowledge-mcp-host-agent
docker rm knowledge-mcp-host-agent
docker stop -t 30 knowledge-mcp
docker rm knowledge-mcp
docker compose up -d
```

Keep those volumes; removing them loses the dashboard settings, indexing state, synchronized documents, and Qdrant data. If the old container had a read-only document mount, add that mount to the same override file before starting Compose.

## Select a host folder without changing Docker

With the standard Compose setup on Linux/NAS:

1. Open the dashboard. Login is optional during onboarding.
2. In **Document folders**, enter an absolute folder path, such as `/mnt/documents`, `/mnt/knowledge`, or `/home/user/Documents`, and click **+**. The agent checks that it can reach the folder before adding it to the list. You can add or remove multiple folders under the same non-root parent.
3. Save the indexing policy, then press **Scan** in **Eligible documents**. Continue when the scan finds eligible files.

Compose starts the host agent automatically and the app provides its connection credentials through a private Docker volume. Python, native host services, downloads, and manual pairing are not required. With **Automatic** document location, the root is interpreted on the Linux host. To use an existing app-container mount instead, explicitly select **Folder already mounted inside the container**.

The agent receives the Linux host filesystem at `/host` through a [read-only bind mount](https://docs.docker.com/engine/storage/bind-mounts/#use-a-read-only-bind-mount). It reads only the selected folders, applies the indexing policy before transfer, and copies changed eligible documents into the app's persistent storage. Host symlinks are resolved within the host filesystem and restricted directories remain excluded. `/proc`, `/sys`, `/dev`, and `/run` are masked. The agent has no Docker socket and publishes no ports.

Changing the root from the dashboard requires no Docker configuration changes or container restart. The source stays at the chosen host path; indexing uses the synchronized copy. Mount external drives and network shares on the NAS before starting the stack so they are included in the agent's host mount. On kernels before Linux 5.12, Docker may retain write access on nested mounts despite a read-only parent mount; use Linux 5.12+ for recursive read-only protection.

The separate **Eligible documents** step shows connection errors, files checked, transfer progress, and the eligible count after you press **Scan**. Its popup shows every eligible file and supports TXT, LOG, and JSON downloads. After onboarding, the agent rescans every five minutes and responds to **Sync folder now**. It pauses synchronization while indexing runs. Scheduled indexing uses the existing incremental hash state, so unchanged documents are not embedded again.

Check the agent with `docker compose logs host-agent`. To restart it, use `docker compose restart host-agent`. The connection credentials are retained in `host_agent_data`, and the app stores only their hash in `app_data`. A Compose upgrade starts the agent without requiring user installation. Keep the existing app and Qdrant volumes during migration.

This automatic setup targets Docker Engine on Linux/NAS. Docker Desktop on Windows or macOS requires separate native filesystem sharing and is not covered by mounting the Linux root. The README's two-container `docker run` setup provides the same Linux host access without a repository checkout.

## Compose with an existing document folder

This is an optional alternative to the host agent. Create `compose.override.yaml` beside `compose.yaml`:

```yaml
services:
  app:
    volumes:
      - type: bind
        source: /absolute/path/to/documents
        target: /knowledge
        read_only: true
        bind:
          create_host_path: false
```

Replace `source` with an existing absolute host path, then run `docker compose up -d`. In **Document folders**, choose **Folder already mounted inside the container**, enter `/knowledge`, and click **+** to check and add it. On Windows, use a host source such as `C:/Users/Name/Documents`. A bind mount still requires a container recreation when changed.

To select folders from different host locations, mount each one under a separate subdirectory of the same container root (for example `/knowledge/team-a` and `/knowledge/team-b`), then add each mounted folder in the dashboard.

The dashboard health check is also available at `/api/health` on port 8080 and returns an error while Qdrant is unavailable. The image uses it for its Docker health status. Scheduled indexing rescans the document root and skips parsing and embedding files already recorded unchanged in the persistent ingestion state.

## Upgrade from the older two-container Compose stack

The earlier Compose file ran Qdrant as a separate service. The new image contains Qdrant and mounts the **same** `qdrant_data` volume at `/qdrant/storage`; the `app_data` volume is also reused when the Compose project name stays the same. Stop the old stack before updating the repository so the old and new Qdrant processes never write to the same volume at once:

```bash
docker compose down
git pull
docker compose pull
docker compose up -d
```

Do not add `-v` to `docker compose down`. If you previously used `KNOWLEDGE_HOST_PATH`, create the read-only mount override above with that same folder before running a full index. A full index removes documents absent from its selected source. If the repository directory or Compose project name changes, Docker may create new volumes instead of reusing the old ones; check volume names before indexing.

A separately started Qdrant container is not part of this Compose project and is not imported automatically. Keep its container, volume, and ingestion state until you have verified a migration.
