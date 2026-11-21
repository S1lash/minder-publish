"""Exercise the real hidden terminal prompt against a disposable local server."""
import json
import os
import select
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


@pytest.mark.skipif(os.name == 'nt', reason='POSIX PTY; Windows private input requires native acceptance')
def test_new_client_setup_private_prompt_and_reuse(tmp_path):
    import errno
    import pty

    synthetic_token = 'synthetic-only-device-token'
    requests = []

    class Fixture(BaseHTTPRequestHandler):
        def do_GET(self):
            authorized = self.path == '/api/v1/capabilities' and self.headers.get('Authorization') == 'Bearer ' + synthetic_token
            requests.append(authorized)
            payload = json.dumps({'data': {'fixture': True}} if authorized else {'error': {'code': 'invalid_credentials'}}).encode()
            self.send_response(200 if authorized else 401)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    home = tmp_path / 'home'
    home.mkdir()
    env = dict(os.environ, HOME=str(home), XDG_CONFIG_HOME=str(home / 'config'), NO_PROXY='127.0.0.1,localhost')
    for key in ('MINDER_PUBLISH_URL', 'MINDER_PUBLISH_TOKEN', 'MINDER_PUBLISH_TOKEN_FILE', 'MINDER_PUBLISH_CONFIG_DIR', 'MINDER_PUBLISH_INSTANCE'):
        env.pop(key, None)
    command = [sys.executable, '-m', 'minder_publish.client.cli', '--json', 'setup', '--runtime', 'codex', '--url', f'http://127.0.0.1:{server.server_port}', '--name', 'fixture', '--connect']

    def execute(*, enter_token):
        master, slave = pty.openpty()
        process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True)
        os.close(slave)
        output = bytearray()
        sent = False
        deadline = time.monotonic() + 20
        try:
            while time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], .2)
                if ready:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError as error:
                        if error.errno == errno.EIO:
                            break
                        raise
                    if not chunk:
                        break
                    output.extend(chunk)
                    if b'Device token:' in output and not sent:
                        assert enter_token, 'reused profile must not prompt'
                        os.write(master, synthetic_token.encode() + b'\n')
                        sent = True
                if process.poll() is not None and not ready:
                    break
            assert process.wait(timeout=2) == 0, output.decode(errors='replace')
            assert synthetic_token.encode() not in output, 'private prompt echoed token'
            return bytes(output), sent
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            os.close(master)

    try:
        first, sent = execute(enter_token=True)
        assert sent and b'Device token:' in first
        result = json.loads(first[first.index(b'{'):])['data']
        assert result['authenticated'] and not result['reused_configuration']
        config_directory = home / 'config/minder-publish'
        profile = json.loads((config_directory / 'config.json').read_text())['instances']['fixture']
        secret_file = config_directory / profile['token_file']
        assert secret_file.read_text().strip() == synthetic_token
        assert secret_file.stat().st_mode & 0o777 == 0o600
        second, sent = execute(enter_token=False)
        assert not sent and b'Device token:' not in second
        result = json.loads(second[second.index(b'{'):])['data']
        assert result['authenticated'] and result['reused_configuration']
        assert requests == [True, True]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
