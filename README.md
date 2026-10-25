# Knowledge MCP

![Under development](https://img.shields.io/badge/status-under%20development-orange)
[![M8ven Score](https://m8ven.ai/badge/mcp/paoloronco/knowledge-mcp)](https://m8ven.ai/mcp/paoloronco/knowledge-mcp?s=readme)
[![M8ven Score](https://m8ven.ai/badge/mcp/paoloronco-knowledge-mcp-14ovm2?v=28c52f88d755d516c4016cf8e421ba54)](https://m8ven.ai/mcp/paoloronco-knowledge-mcp-14ovm2?s=readme)

Give your AI client access to the documents you choose. Knowledge MCP indexes your files and provides an MCP search tool that returns relevant passages, source paths, and page or slide references when available.

Embeddings and search run locally. The Docker image includes a browser dashboard, the indexing service, the MCP server, and Qdrant.

## Features

- **Web dashboard:** select folders, choose a model, follow indexing progress, and manage services.
- **MCP search:** combine semantic search with keyword matching and return passages with their sources.
- **Bundled Qdrant:** store document vectors without deploying a separate database container.
- **Document formats:** PDF, Word (`.docx`), PowerPoint (`.pptx`), Markdown, plain text, and HTML.
- **Incremental indexing:** process changed documents, remove deleted documents from the index, and schedule updates. A full rebuild is also available.
- **Local embedding models:** multilingual E5 small, E5 base, and BGE-M3, with CPU or NVIDIA GPU execution.
- **Backup and restore:** export settings and saved indexes, then import them on another instance.
- **Read-only source access:** the Linux companion reads host folders and synchronizes eligible files into the app's storage.

### Addresses

Replace `HOST_IP` with the IP address or hostname of the machine running Docker.

| Service | Address | Purpose |
| --- | --- | --- |
| Dashboard | `http://HOST_IP:8080` | Setup and administration |
| MCP | `http://HOST_IP:8000/mcp` | Connect an MCP-compatible client after indexing and starting MCP |
| Qdrant | `127.0.0.1:6333` inside the app container | Internal vector database; no host port is published |

## Docker image

The commands below use Docker Engine on a **Linux host or NAS**. Start the app first, then its companion.

### Standard image — CPU

```bash
docker pull paoloronco/knowledge-mcp:latest
docker run -d --name knowledge-mcp --restart unless-stopped \
  -p 8080:8080 -p 8000:8000 \
  -e AUTO_HOST_AGENT_CONFIG=/run/host-agent/agent.json \
  -v knowledge_app:/data \
  -v knowledge_qdrant:/qdrant/storage \
  -v knowledge_agent:/run/host-agent \
  paoloronco/knowledge-mcp:latest
```

- Open the dashboard at `http://HOST_IP:8080`.
- Qdrant starts with the app. Start MCP from the dashboard after indexing.
- The named volumes retain settings and indexes when you recreate the container.
- Docker restarts the containers after a host reboot, unless you stopped them manually.

### Companion — access folders on the Linux host

The companion lets you enter host paths such as `/mnt/documents` in the dashboard. Run it on the same machine as the app:

```bash
docker run -d --name knowledge-mcp-host-agent --restart unless-stopped \
  --network container:knowledge-mcp --read-only \
  --cap-drop ALL --cap-add DAC_READ_SEARCH \
  --security-opt no-new-privileges \
  -v knowledge_agent:/run/host-agent:ro \
  -v /:/host:ro \
  --tmpfs /host/proc:ro,noexec,nosuid,size=1m \
  --tmpfs /host/sys:ro,noexec,nosuid,size=1m \
  --tmpfs /host/dev:ro,noexec,nosuid,size=1m \
  --tmpfs /host/run:ro,noexec,nosuid,size=1m \
  paoloronco/knowledge-mcp:latest \
  python host_agent.py run --config /run/host-agent/agent.json --host-root /host --log-stdout
```

- It reads the selected folders, applies the indexing policy, and copies eligible files into the app volume.
- The host filesystem is mounted read-only; `/proc`, `/sys`, `/dev`, and `/run` are masked.
- It shares the app's network and connection volume. It publishes no ports and uses no Docker socket.
- Folder changes in the dashboard do not require editing the Docker command.

In the CPU setup, both containers use `:latest`: `knowledge-mcp` runs the application; `knowledge-mcp-host-agent` runs the companion.

If your documents are already mounted inside the app container, select **Folder already mounted inside the container** in the dashboard. See [read-only document mounts](docs/docker.md#compose-with-an-existing-document-folder) for that setup, including Windows paths. The Linux companion command does not provide native Windows or macOS filesystem access.

### NVIDIA GPU — CUDA image

Install an NVIDIA driver and the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html) on the Linux Docker host. Then use `:cuda` and `--gpus all` for the app:

```bash
docker pull paoloronco/knowledge-mcp:cuda
docker run -d --name knowledge-mcp --restart unless-stopped --gpus all \
  -p 8080:8080 -p 8000:8000 \
  -e AUTO_HOST_AGENT_CONFIG=/run/host-agent/agent.json \
  -v knowledge_app:/data \
  -v knowledge_qdrant:/qdrant/storage \
  -v knowledge_agent:/run/host-agent \
  paoloronco/knowledge-mcp:cuda
```

- The companion still uses `:latest`; it needs no GPU.
- The dashboard shows the GPU, driver, available memory, and whether CUDA is usable.
- The CUDA image falls back to CPU if no GPU is available.
- Add `-e EMBEDDING_DEVICE=cpu` to force CPU, or `-e EMBEDDING_DEVICE=cuda` to require CUDA.

For an existing installation, follow [the update steps](#update-without-losing-data) before switching images. Keep the same volumes.

<details>
<summary>Check that Docker can access the GPU</summary>

```bash
docker run --rm --gpus all ubuntu nvidia-smi
```

</details>

### Docker Compose

If you prefer Compose, clone the repository and run these commands from its root:

```bash
docker compose up -d
```

For the CUDA image:

```bash
docker compose -f compose.yaml -f compose.gpu.yaml up -d
```

See [Docker deployment notes](docs/docker.md) for volume mappings, updates, and migration from a `docker run` installation.

## Use the dashboard

### First setup

1. Open `http://HOST_IP:8080`. Choose **Continue without login** or **Require a password**, then continue. You can change this later in Settings.
2. Add your document folders. With the companion, use paths on the Linux host, such as `/mnt/documents`.
3. Review the indexing policy: file types, size limits, and excluded paths.
4. Run **Scan** to check eligible files, then the **Dry-run test** to check document parsing.
5. Choose an embedding model and start **Initial indexing**. You can also select **Skip for now** and index later.
6. Once indexing succeeds, open **Dashboard → MCP server → Start** and connect your client to `http://HOST_IP:8000/mcp`.

### Sections

| Section | What you can do |
| --- | --- |
| **Dashboard** | Check service health and start, stop, or restart Qdrant and MCP |
| **Indexing → Embedding model** | Select the model for the next indexing run |
| **Indexing → Indexing** | Run a dry test, update incrementally, rebuild, inspect errors, and download logs |
| **Document folders → Documents and sync** | Add or remove folders and request synchronization |
| **Document folders → Schedule** | Set the update interval; `0` disables scheduled indexing |
| **Indexing policy** | Choose allowed file types and exclusions |
| **Settings** | Manage the dashboard password, download backups, and restore saved data |

Indexing shows the current stage, document count, and progress. At completion, the date and final count appear together. The full output and timestamped status log are available to download.

If a document fails, fix its source or use **Ignore this file**, then retry the incremental update. Successfully indexed documents stay saved.

#### Rebuild from scratch

- Recreates the selected model's index and processes every eligible document again.
- Requires confirmation and pauses MCP search.
- Keeps source files and other models' indexes.
- A failed rebuild can leave a partial index. Review the errors and retry incrementally.

### Embedding models

| Model | Vector dimensions | CPU and memory demand |
| --- | --- | --- |
| [E5 multilingual small](https://huggingface.co/intfloat/multilingual-e5-small) | 384 | Lowest; the default |
| [E5 multilingual base](https://huggingface.co/intfloat/multilingual-e5-base) | 768 | Medium |
| [BGE-M3](https://huggingface.co/BAAI/bge-m3) | 1024 | Highest |

- Models download on first use and remain cached in the app volume.
- Each model has a separate index. Search switches to a newly selected model after a successful indexing run.
- Switching back reuses that model's index. Keeping several indexes uses more disk space.
- The dry run checks parsing only; it does not load the embedding model.
- BGE-M3 uses dense vectors in this app. The resource labels are relative guidance, not benchmark results.

## Update without losing data

For the `docker run` installation above:

1. Pull the CPU/companion image:

   ```bash
   docker pull paoloronco/knowledge-mcp:latest
   ```

   If your app uses CUDA, also pull:

   ```bash
   docker pull paoloronco/knowledge-mcp:cuda
   ```

2. Stop and remove the companion, then the app container:

   ```bash
   docker stop -t 30 knowledge-mcp-host-agent
   docker rm knowledge-mcp-host-agent
   docker stop -t 30 knowledge-mcp
   docker rm knowledge-mcp
   ```

3. Repeat the app and companion commands from [Docker image](#docker-image), using the **same named volumes**.

Recreate the companion whenever you recreate the app: it joins the app container's network.

| Volume | Saved data |
| --- | --- |
| `knowledge_app` | Settings, indexing policy and state, synchronized documents, model cache, and dashboard login |
| `knowledge_qdrant` | Qdrant indexes for all models |
| `knowledge_agent` | Companion connection credentials |

**Keep these volumes.** Removing the containers preserves them; deleting the volumes removes your saved data. Compose users should follow the [Compose update instructions](docs/docker.md#linuxnas-deployment).

## Backup and restore

Open **Settings → Backup and restore** and use **Download backup** to save a `.tar.gz` archive. On the destination instance, open Settings, choose the file and select **Import backup and replace data**. The setup screen also links to Settings, so you can restore before onboarding.

- **Included:** settings, indexing policy and state, Qdrant indexes, synchronized documents, and the companion connection.
- **Excluded:** downloaded model caches, external source folders, Docker mounts, port/GPU settings, and custom environment variables.
- Wait for indexing and synchronization to finish. Search pauses during export.
- Restore replaces the destination's data and restarts the app. Use the dashboard password saved in the backup.
- Restore to the same app version and storage layout first. The Qdrant version must match the version recorded in the archive.
- Make the original source paths available on the destination before resuming synchronization or indexing.

The archive contains documents and credentials and is **not encrypted**. Keep it private. See [backup and migration details](docs/docker.md#downloading-and-importing-a-backup) for validation, limits, and recovery.

## Install from source / use the code

Use this setup to run ingestion and the MCP server directly in Python. Qdrant runs separately.

### Requirements

- Python 3.10 or newer.
- Qdrant reachable at `http://127.0.0.1:6333`.
- An existing document directory, readable by your user.

### Install and configure

The commands below use Bash:

```bash
git clone https://github.com/paoloronco/knowledge-mcp.git knowledge-mcp-src
cd knowledge-mcp-src/knowledge-mcp
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

On Windows PowerShell, use `.venv\Scripts\Activate.ps1` to activate the environment and `Copy-Item .env.example .env` to copy the configuration.

Edit `.env`:

```dotenv
KNOWLEDGE_ROOT=/absolute/path/to/documents
QDRANT_URL=http://127.0.0.1:6333
MCP_HOST=127.0.0.1
MCP_PORT=8000
```

Review [index-policy.yaml](knowledge-mcp/mcp/index-policy.yaml) before running ingestion. Both ingestion and the server load `.env` from the `knowledge-mcp/` service directory.

<details>
<summary>Start a local Qdrant instance with Docker</summary>

This uses the same Qdrant version as the app's [Dockerfile](knowledge-mcp/Dockerfile):

```bash
docker run -d --name knowledge-qdrant --restart unless-stopped \
  -p 127.0.0.1:6333:6333 \
  -v knowledge_source_qdrant:/qdrant/storage \
  qdrant/qdrant:v1.19.1
```

</details>

### Index and start MCP

From the service directory:

```bash
python ingestion/ingest.py --dry-run --limit 10
python ingestion/ingest.py
python mcp/server.py
```

The endpoint is `http://127.0.0.1:8000/mcp`. Restart the server after subsequent ingestion runs so its cached search data includes the changes.

### Code map

| File | Role |
| --- | --- |
| [ingestion/ingest.py](knowledge-mcp/ingestion/ingest.py) | Parse documents, create embeddings, and update Qdrant |
| [mcp/server.py](knowledge-mcp/mcp/server.py) | Expose the MCP tools over Streamable HTTP |
| [mcp/retrieval.py](knowledge-mcp/mcp/retrieval.py) | Search and rank document passages |
| [mcp/webui.py](knowledge-mcp/mcp/webui.py) | Dashboard API and service management |
| [mcp/index-policy.yaml](knowledge-mcp/mcp/index-policy.yaml) | File selection and exclusions |

## Access and document privacy

- Dashboard login is optional. Without it, anyone who can reach port 8080 can administer the app.
- The MCP endpoint has no built-in HTTP authentication. Use an authenticated proxy or [Cloudflare Access](docs/cloudflare-access.md) before exposing it beyond a trusted LAN.
- Review the indexing policy before adding documents. Credential redaction is best effort; do not rely on it to make sensitive files safe to index.

## Documentation

- [Docker deployment, updates, and migration](docs/docker.md)
- [Architecture](docs/architecture.md)
- [Retrieval behavior](docs/retrieval.md)
- [Security model](docs/security-model.md)
- [Troubleshooting](docs/troubleshooting.md)
- [AI client examples](AI/README.md)
- [Linux service deployment](knowledge-mcp/mcp/deployment/README.md)
