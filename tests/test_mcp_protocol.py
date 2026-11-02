"""Exercise real MCP negotiation, schemas and navigation through Streamable HTTP."""

import asyncio
import contextlib
import io
import json
import os
import socket
import sys
import tempfile
import types
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

import uvicorn
from qdrant_client import QdrantClient
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parents[1] / "knowledge-mcp"
sys.path.insert(0, str(ROOT / "ingestion"))
sys.path.insert(0, str(ROOT / "mcp"))
import ingest
import retrieval
import server as knowledge_server
from test_core import FakeModel


class MCPProtocolTest(unittest.IsolatedAsyncioTestCase):
    async def test_streamable_http_search_browse_read_and_validation(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            base = Path(tmp)
            docs = base / "documents"
            docs.mkdir()
            (docs / "network.md").write_text(
                "# Rete osservatorio\nIl telescopio usa la VLAN 42 sulla porta 8443.\n# Storage\nI backup usano snapshot giornalieri.",
                encoding="utf-8",
            )
            (docs / "database.md").write_text(
                "# Telemetria\nQdrant conserva gli eventi di calibrazione stellare.",
                encoding="utf-8",
            )
            client = QdrantClient(":memory:")
            stack.callback(client.close)
            real_model = os.getenv("KNOWLEDGE_TEST_REAL_MODEL") == "1"
            if real_model:
                from sentence_transformers import SentenceTransformer

                model = SentenceTransformer(
                    "intfloat/multilingual-e5-small",
                    device="cpu",
                    local_files_only=True,
                )
                model_class = lambda *args, **kwargs: model
            else:
                model, model_class = FakeModel(), FakeModel
            for p in [
                patch.dict(
                    os.environ,
                    {"KNOWLEDGE_ROOT": str(docs), "INDEX_SOURCE_PATHS": '[""]'},
                ),
                patch.dict(
                    sys.modules,
                    {
                        "sentence_transformers": types.SimpleNamespace(
                            SentenceTransformer=model_class
                        )
                    },
                ),
                patch.object(ingest, "QdrantClient", return_value=client),
                patch.object(ingest, "embedding_device", return_value="cpu"),
                patch.object(ingest, "STATE_FILE", base / "state.json"),
                patch.object(ingest, "ERROR_LOG", base / "errors.log"),
                patch.object(retrieval, "client", client),
                patch.object(retrieval, "model", model),
                patch.object(knowledge_server, "client", client),
                patch.object(sys, "argv", ["ingest.py"]),
            ]:
                stack.enter_context(p)
            with (
                warnings.catch_warnings(),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                warnings.filterwarnings(
                    "ignore", message="Payload indexes have no effect"
                )
                ingest.main()
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            web = uvicorn.Server(
                uvicorn.Config(
                    knowledge_server.mcp.streamable_http_app(),
                    host="127.0.0.1",
                    port=port,
                    log_level="error",
                )
            )
            task = asyncio.create_task(web.serve(sockets=[listener]))
            try:
                deadline = asyncio.get_running_loop().time() + 15
                while not web.started:
                    if asyncio.get_running_loop().time() > deadline:
                        raise TimeoutError("MCP HTTP server did not start")
                    await asyncio.sleep(0.01)
                async with streamable_http_client(
                    f"http://127.0.0.1:{port}/mcp"
                ) as streams:
                    async with ClientSession(streams[0], streams[1]) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        self.assertEqual(
                            {t.name for t in tools.tools},
                            {
                                "knowledge_status",
                                "search_private_knowledge",
                                "browse_private_knowledge",
                                "read_private_document",
                            },
                        )

                        async def call(name, args):
                            response = await session.call_tool(name, args)
                            self.assertFalse(response.is_error, response)
                            return response.structured_content or json.loads(
                                response.content[0].text
                            )

                        status = await call("knowledge_status", {})
                        self.assertEqual(status["status"], "ok")
                        self.assertFalse(status["index_update_required"])
                        listing = await call("browse_private_knowledge", {"limit": 1})
                        self.assertEqual(len(listing["documents"]), 1)
                        result = await call(
                            "search_private_knowledge",
                            {
                                "query": "Quale VLAN usa il telescopio?",
                                "source": "network.md",
                            },
                        )
                        self.assertIn("VLAN 42", result["results"][0]["text"])
                        identity = result["results"][0]["document_id"]
                        first = await call(
                            "read_private_document",
                            {"document_id": identity, "limit": 1},
                        )
                        second = await call(
                            "read_private_document",
                            {
                                "document_id": identity,
                                "start_chunk": first["next_chunk"],
                                "limit": 1,
                            },
                        )
                        self.assertEqual(second["chunks"][0]["section"], "Storage")
                        self.assertIsNone(second["next_chunk"])
                        invalid = await session.call_tool(
                            "browse_private_knowledge", {"path_prefix": "../private"}
                        )
                        self.assertTrue(invalid.is_error)
                        if real_model:
                            semantic = await call(
                                "search_private_knowledge",
                                {
                                    "query": "Where are the star calibration events stored?"
                                },
                            )
                            self.assertEqual(
                                semantic["results"][0]["source"], "database.md"
                            )
            finally:
                web.should_exit = True
                await task
                listener.close()


if __name__ == "__main__":
    unittest.main()
