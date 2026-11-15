import gzip
import io
import json
import os
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "knowledge-mcp" / "mcp"))
import backup
import webui as app

QDRANT_BINARY = Path(os.getenv('QDRANT_TEST_BINARY', '/qdrant/qdrant'))


def replace_archive_files(contents, replacements):
    output = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(contents), mode='r:gz') as source, tarfile.open(fileobj=output, mode='w:gz') as target:
        for member in source:
            if member.name not in replacements:
                target.addfile(member, source.extractfile(member) if member.isfile() else None)
        for name, content in replacements.items():
            payload = content.encode()
            item = tarfile.TarInfo(name)
            item.size = len(payload)
            target.addfile(item, io.BytesIO(payload))
    return output.getvalue()


class BackupTest(unittest.TestCase):
    def test_round_trip_validation_and_rollback(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            roots = {name: base / name for name in ("data", "qdrant", "agent")}
            for root in roots.values():
                root.mkdir()
            (roots["data"] / "config.json").write_text('{}')
            (roots["data"] / "index-policy.yaml").write_text('policy')
            (roots["data"] / "ingestion").mkdir()
            (roots["data"] / "ingestion" / "state.json").write_text('hashes')
            (roots["data"] / "huggingface").mkdir()
            (roots["data"] / "huggingface" / "model.bin").write_bytes(b'cache')
            (roots["qdrant"] / "vectors.bin").write_bytes(b'vectors')
            os.link(roots["qdrant"] / "vectors.bin", roots["qdrant"] / "linked.bin")
            (roots["agent"] / "agent.json").write_text('credentials')
            archive = base / 'backup.tar.gz'
            backup.create_backup(archive, roots)
            stages = backup.stage_restore(archive, roots)
            self.assertFalse((stages["data"] / "huggingface").exists())
            (roots["qdrant"] / "vectors.bin").write_bytes(b'current')
            original_rename = Path.rename
            failed = False

            def fail_once(path, destination):
                nonlocal failed
                if not failed and path == stages["qdrant"] / "vectors.bin":
                    failed = True
                    raise OSError('simulated disk error')
                return original_rename(path, destination)

            with patch.object(Path, 'rename', fail_once), self.assertRaises(OSError):
                backup.apply_restore(stages, roots)
            self.assertEqual((roots["qdrant"] / "vectors.bin").read_bytes(), b'current')
            self.assertEqual((roots["agent"] / "agent.json").read_text(), 'credentials')
            backup.apply_restore(stages, roots)
            self.assertEqual((roots["qdrant"] / "vectors.bin").read_bytes(), b'vectors')
            self.assertEqual((roots["qdrant"] / "linked.bin").read_bytes(), b'vectors')
            self.assertEqual((roots["data"] / "ingestion" / "state.json").read_text(), 'hashes')
            for name, kind in [('data/../escape', tarfile.REGTYPE), ('data/link', tarfile.SYMTYPE), ('data/C:/escape', tarfile.REGTYPE), ('data\\escape', tarfile.REGTYPE)]:
                with tarfile.open(archive, 'w:gz') as output:
                    manifest = json.dumps({'format': 'knowledge-mcp-backup', 'version': backup.FORMAT_VERSION, 'roots': sorted(roots), 'qdrant_version': None}).encode()
                    item = tarfile.TarInfo('manifest.json')
                    item.size = len(manifest)
                    output.addfile(item, io.BytesIO(manifest))
                    item = tarfile.TarInfo(name)
                    item.type = kind
                    item.linkname = '/etc/passwd'
                    output.addfile(item)
                with self.assertRaises(ValueError):
                    backup.stage_restore(archive, roots)
                self.assertEqual((roots["qdrant"] / "vectors.bin").read_bytes(), b'vectors')
                self.assertFalse(list(roots["data"].glob('.restore-*')))

    def test_duplicate_normalized_paths_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            roots = {name: base / name for name in ('data', 'qdrant', 'agent')}
            for root in roots.values():
                root.mkdir()
            archive = base / 'backup.tar.gz'
            with tarfile.open(archive, 'w:gz') as output:
                manifest = json.dumps({'format': 'knowledge-mcp-backup', 'version': backup.FORMAT_VERSION, 'roots': sorted(roots), 'qdrant_version': None}).encode()
                item = tarfile.TarInfo('manifest.json')
                item.size = len(manifest)
                output.addfile(item, io.BytesIO(manifest))
                root = tarfile.TarInfo('data')
                root.type = tarfile.DIRTYPE
                output.addfile(root)
                for name in ('data/config.json', 'data//config.json'):
                    payload = b'content'
                    item = tarfile.TarInfo(name)
                    item.size = len(payload)
                    output.addfile(item, io.BytesIO(payload))
            with self.assertRaisesRegex(ValueError, 'unsafe path or file type'):
                backup.stage_restore(archive, roots)
            self.assertFalse(list(base.glob('**/.restore-*')))

    def test_http_export_import_auth_and_service_pause(self):
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            base = Path(folder)
            data = base / 'data'
            roots = {'data': data, 'qdrant': base / 'qdrant', 'agent': base / 'agent'}
            for root in roots.values():
                root.mkdir()
            for name, value in {
                'DATA': data, 'SOURCE': data / 'documents', 'MANAGED_SOURCE': data / 'documents',
                'HOST_SOURCE': data / 'host-documents', 'CONFIG': data / 'config.json',
                'POLICY': data / 'index-policy.yaml', 'AUTH': data / 'auth.json',
                'AGENT_AUTH': data / 'agent-auth.json', 'AGENT_MANIFEST': data / 'agent-manifest.json',
                'AUTO_AGENT_CONFIG': roots['agent'] / 'agent.json',
            }.items():
                stack.enter_context(patch.object(app, name, value))
            stack.enter_context(patch.object(app.Controller, '_schedule', lambda self: None))
            controller = app.Controller()
            stack.enter_context(patch.object(app, 'controller', controller))
            stack.enter_context(patch.object(controller, 'backup_roots', return_value=roots))
            stack.enter_context(patch.object(controller, 'qdrant_backup_version', return_value='qdrant test-version'))
            for name in ('_stop_mcp', '_stop_qdrant', '_start_mcp', '_start_qdrant'):
                stack.enter_context(patch.object(controller, name))
            controller.qdrant = Mock()
            controller.qdrant.poll.return_value = None
            controller.mcp = Mock()
            controller.mcp.poll.return_value = None
            controller.config['last_run_at'] = 123.5
            (roots['qdrant'] / 'vectors.bin').write_bytes(b'vectors')
            server = ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            url = f'http://127.0.0.1:{server.server_port}'

            def post(route, contents=b'', token=app.CONTROL_TOKEN):
                return urllib.request.urlopen(urllib.request.Request(url + route, data=contents, headers={'X-Control-Token': token}), timeout=5)

            try:
                with self.assertRaises(urllib.error.HTTPError) as denied:
                    post('/api/backup', token='wrong')
                self.assertEqual(denied.exception.code, 403)
                controller.sync_in_progress = True
                with self.assertRaises(urllib.error.HTTPError):
                    post('/api/backup')
                controller.sync_in_progress = False
                with post('/api/backup') as response:
                    download = json.load(response)['url']
                with urllib.request.urlopen(url + download) as response:
                    self.assertEqual(response.headers['Content-Type'], 'application/gzip')
                    contents = response.read()
                with self.assertRaises(urllib.error.HTTPError) as consumed:
                    urllib.request.urlopen(url + download)
                self.assertEqual(consumed.exception.code, 404)
                self.assertIsNone(controller.backup_download)
                controller._stop_qdrant.assert_called_once()
                controller._start_qdrant.assert_called_once()
                controller._start_mcp.assert_called_once()
                with self.assertRaisesRegex(RuntimeError, 'export failed'), controller.backup_pause():
                    raise RuntimeError('export failed')
                self.assertEqual(controller._start_qdrant.call_count, 2)
                self.assertEqual(controller._start_mcp.call_count, 2)
                app.AUTH.write_text('{}')
                with self.assertRaises(urllib.error.HTTPError) as unauthenticated:
                    post('/api/backup')
                self.assertEqual(unauthenticated.exception.code, 403)
                app.AUTH.unlink()
                with self.assertRaises(urllib.error.HTTPError) as invalid:
                    post('/api/restore', b'invalid archive')
                self.assertEqual(invalid.exception.code, 400)
                self.assertEqual((roots['qdrant'] / 'vectors.bin').read_bytes(), b'vectors')
                saved_config = dict(controller.config)
                for setting, bad_value in [('interval_hours', float('nan')), ('folders', ['../escape']), ('active_embedding_model', {}), ('setup_step', True)]:
                    tampered = replace_archive_files(contents, {'data/config.json': json.dumps({**saved_config, setting: bad_value})})
                    with self.assertRaises(urllib.error.HTTPError) as invalid:
                        post('/api/restore', tampered)
                    self.assertEqual(invalid.exception.code, 400)
                for filename, content in [('data/auth.json', '{}'), ('data/agent-auth.json', '{}'), ('data/index-policy.yaml', '{}'), ('data/agent-manifest.json', '{"revision":0,"files":[] }')]:
                    with self.assertRaises(urllib.error.HTTPError) as invalid:
                        post('/api/restore', replace_archive_files(contents, {filename: content}))
                    self.assertEqual(invalid.exception.code, 400)
                self.assertEqual((roots['qdrant'] / 'vectors.bin').read_bytes(), b'vectors')
                self.assertEqual(controller._stop_qdrant.call_count, 2)
                controller.config['last_run_at'] = 0
                controller.config['active_embedding_model'] = 'e5-small'
                (roots['qdrant'] / 'vectors.bin').write_bytes(b'changed')
                with post('/api/restore', contents) as response:
                    self.assertTrue(json.load(response)['restarting'])
                worker.join(timeout=5)
                self.assertFalse(worker.is_alive())
                self.assertTrue(controller.stopping)
                self.assertTrue(controller.restart_requested)
                self.assertEqual((roots['qdrant'] / 'vectors.bin').read_bytes(), b'vectors')
                self.assertEqual(json.loads(app.CONFIG.read_text())['last_run_at'], 123.5)
                self.assertEqual(controller._start_qdrant.call_count, 2)
            finally:
                server.shutdown()
                server.server_close()

    def test_corruption_limits_missing_volumes_and_version_mismatch(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            roots = {'data': base / 'data', 'qdrant': base / 'qdrant'}
            for root in roots.values():
                root.mkdir()
            (roots['data'] / 'config.json').write_text('{}')
            (roots['data'] / 'index-policy.yaml').write_text('policy')
            (roots['qdrant'] / 'vectors.bin').write_bytes(b'vectors')
            path = base / 'backup.tar.gz'
            backup.create_backup(path, roots, 'version-a')
            contents = path.read_bytes()
            for damaged in (contents[:-4], contents[:-8] + bytes([contents[-8] ^ 1]) + contents[-7:]):
                path.write_bytes(damaged)
                with self.assertRaisesRegex(ValueError, 'corrupt or incomplete'):
                    backup.stage_restore(path, roots, 'version-a')
                self.assertFalse(list(roots['data'].glob('.restore-*')))
                self.assertEqual((roots['qdrant'] / 'vectors.bin').read_bytes(), b'vectors')
            path.write_bytes(contents)
            path.write_bytes(gzip.compress(gzip.decompress(contents) + b'\0' * (2 * 1024 * 1024)))
            with self.assertRaisesRegex(ValueError, 'trailing data'):
                backup.stage_restore(path, roots, 'version-a')
            path.write_bytes(contents)
            with self.assertRaisesRegex(ValueError, 'Qdrant version'):
                backup.stage_restore(path, roots, 'version-b')
            with patch.object(backup, 'MAX_UNPACKED_SIZE', 1), self.assertRaises(ValueError):
                backup.stage_restore(path, roots, 'version-a')
            with patch.object(backup, 'MAX_MEMBERS', 1), self.assertRaises(ValueError):
                backup.stage_restore(path, roots, 'version-a')
            with patch.object(backup, 'MAX_MEMBERS', 1), self.assertRaises(ValueError):
                backup.create_backup(base / 'too-large.tar.gz', roots)
            incomplete = base / 'incomplete.tar.gz'
            with tarfile.open(path, 'r:gz') as source, tarfile.open(incomplete, 'w:gz') as target:
                for member in source:
                    if member.name != 'qdrant':
                        target.addfile(member, source.extractfile(member) if member.isfile() else None)
            with self.assertRaisesRegex(ValueError, 'missing a persistent volume'):
                backup.stage_restore(incomplete, roots, 'version-a')

    def test_restore_restarts_the_web_process_without_exiting_the_container(self):
        controller = Mock(restart_requested=True)
        server = Mock()
        with patch.object(app, 'controller', None), patch.object(app, 'Controller', return_value=controller), patch.object(app, 'ThreadingHTTPServer', return_value=server), patch.object(app.signal, 'signal'), patch.object(app.os, 'execv') as restart:
            app.main()
        controller.close.assert_called_once()
        server.server_close.assert_called_once()
        restart.assert_called_once_with(sys.executable, [sys.executable, str(Path(app.__file__).resolve())])

    @unittest.skipUnless(QDRANT_BINARY.is_file(), 'Requires a Qdrant binary; run in the Docker image')
    def test_real_qdrant_vectors_and_queries_survive_restore(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            roots = {'data': base / 'data', 'qdrant': base / 'storage'}
            for root in roots.values():
                root.mkdir()
            (roots['data'] / 'config.json').write_text('{}')
            (roots['data'] / 'index-policy.yaml').write_text(app.DEFAULT_POLICY.read_text(encoding='utf-8'))
            with socket.socket() as http, socket.socket() as grpc:
                http.bind(('127.0.0.1', 0))
                grpc.bind(('127.0.0.1', 0))
                http_port, grpc_port = http.getsockname()[1], grpc.getsockname()[1]
            url = f'http://127.0.0.1:{http_port}'
            env = {**os.environ, 'QDRANT__STORAGE__STORAGE_PATH': str(roots['qdrant']), 'QDRANT__STORAGE__SNAPSHOTS_PATH': str(base / 'snapshots'), 'QDRANT__SERVICE__HOST': '127.0.0.1', 'QDRANT__SERVICE__HTTP_PORT': str(http_port), 'QDRANT__SERVICE__GRPC_PORT': str(grpc_port), 'QDRANT__CLUSTER__ENABLED': 'false'}
            process = None

            def request(route, data=None, method=None):
                contents = json.dumps(data).encode() if data is not None else None
                with urllib.request.urlopen(urllib.request.Request(url + route, data=contents, method=method, headers={'Content-Type': 'application/json'}), timeout=2) as response:
                    return json.load(response)

            def stop():
                if process is not None and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                        raise

            with (base / 'qdrant.log').open('w') as log:
                def start():
                    nonlocal process
                    process = subprocess.Popen([str(QDRANT_BINARY.resolve())], cwd=base, env=env, stdout=log, stderr=subprocess.STDOUT)
                    deadline = time.monotonic() + 20
                    while time.monotonic() < deadline and process.poll() is None:
                        try:
                            with urllib.request.urlopen(url + '/readyz', timeout=1):
                                return
                        except OSError:
                            time.sleep(0.1)
                    raise RuntimeError('Qdrant failed to start: ' + (base / 'qdrant.log').read_text()[-4000:])

                try:
                    version = subprocess.check_output([str(QDRANT_BINARY.resolve()), '--version'], text=True).strip()
                    start()
                    for collection in ('documents', 'documents_bge_m3'):
                        request('/collections/' + collection, {'vectors': {'size': 3, 'distance': 'Cosine'}}, 'PUT')
                        request('/collections/' + collection + '/points?wait=true', {'points': [{'id': 1, 'vector': [1, 0, 0], 'payload': {'text': 'Backup test', 'source_path': 'example.md'}}]}, 'PUT')
                    stop()
                    archive = base / 'backup.tar.gz'
                    backup.create_backup(archive, roots, version)
                    start()
                    request('/collections/documents', method='DELETE')
                    stop()
                    stages = backup.stage_restore(archive, roots, version)
                    backup.apply_restore(stages, roots)
                    start()
                    for collection in ('documents', 'documents_bge_m3'):
                        points = request('/collections/' + collection + '/points/query', {'query': [1, 0, 0], 'limit': 1, 'with_payload': True})['result']['points']
                        self.assertEqual(points[0]['id'], 1)
                        self.assertEqual(points[0]['payload']['text'], 'Backup test')
                        self.assertGreater(points[0]['score'], 0.99)
                finally:
                    stop()


if __name__ == '__main__':
    unittest.main()
