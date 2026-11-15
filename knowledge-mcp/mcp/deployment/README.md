# Linux deployment

This is a manual example for a Linux host with Python 3.10+, Docker and a document directory mounted read-only. Adjust user names and paths for your system. The application does not configure Cloudflare or authentication.

## Qdrant

Run Qdrant with both ports bound to loopback and persistent storage:

~~~bash
docker volume create qdrant_storage
docker run -d --name qdrant --restart unless-stopped \
  -p 127.0.0.1:6333:6333 -p 127.0.0.1:6334:6334 \
  -v qdrant_storage:/qdrant/storage qdrant/qdrant:latest
curl --fail http://127.0.0.1:6333/readyz
~~~

Pin and test a specific Qdrant image version for your deployment before production upgrades.

## Application

Clone [paoloronco/knowledge-mcp](https://github.com/paoloronco/knowledge-mcp) under /opt/knowledge-mcp and create a dedicated service account with read access to the document mount. From /opt/knowledge-mcp/knowledge-mcp:

~~~bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
~~~

Edit .env. Set KNOWLEDGE_ROOT to the mounted read-only document path, INGESTION_BASE_DIR to a writable state directory, and MCP_HOST to 127.0.0.1 if the tunnel or reverse proxy runs on the same host. Restrict .env permissions to the service account. Run a dry run, then a full ingestion:

~~~bash
python ingestion/ingest.py --dry-run --limit 10
python ingestion/ingest.py
~~~

## systemd

Save the following as /etc/systemd/system/knowledge-mcp.service after adjusting paths and user. The service account needs read access to the checkout and state directory.

~~~ini
[Unit]
Description=Knowledge MCP
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=mcp
WorkingDirectory=/opt/knowledge-mcp/knowledge-mcp
ExecStart=/opt/knowledge-mcp/knowledge-mcp/.venv/bin/python mcp/server.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
~~~

~~~bash
sudo systemctl daemon-reload
sudo systemctl enable --now knowledge-mcp
sudo journalctl -u knowledge-mcp -f
~~~

Search reads the live Qdrant collection on each request, so subsequent ingestion runs need no MCP restart. After upgrading from an older release, run one incremental update per model index to add the lexical and navigation indexes. Check Qdrant's /readyz endpoint and call the MCP knowledge_status tool with a real MCP client; a plain GET is not a tool call. Put an authenticated HTTPS proxy or Cloudflare Access in front of any remote endpoint. See the [Cloudflare guide](../../../docs/cloudflare-access.md).
