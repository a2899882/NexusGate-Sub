import http.cookiejar
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from panel.server import App, make_server


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class UnifiedAuthTests(unittest.TestCase):
    def test_one_session_protects_admin_without_blocking_public_subscriptions(self):
        with tempfile.TemporaryDirectory() as directory:
            ng_port = free_port()
            shared = "integration-bridge-key"
            repo = Path(__file__).resolve().parents[2]
            env = {**os.environ, "NG_HOST": "127.0.0.1", "NG_PORT": str(ng_port),
                   "NG_DATA_FILE": str(Path(directory) / "gate.json"),
                   "NG_ADMIN_PASSWORD": "test-gate-password-123", "NG_COOKIE_SECURE": "false",
                   "NG_SUBVAULT_BRIDGE_KEY": shared}
            process = subprocess.Popen(["node", "server.js"], cwd=repo, env=env,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            old_env = os.environ.copy()
            server = None
            thread = None
            try:
                for _ in range(60):
                    try:
                        urllib.request.urlopen(f"http://127.0.0.1:{ng_port}/healthz", timeout=0.2).close()
                        break
                    except (OSError, urllib.error.URLError):
                        time.sleep(0.1)
                else:
                    self.fail("NexusGate failed to start")
                os.environ.update(SUBVAULT_DATA_DIR=directory, SUBVAULT_AUTH_MODE="nexusgate",
                                  SUBVAULT_BRIDGE_KEY=shared, SUBVAULT_NG_PORT=str(ng_port),
                                  SUBVAULT_PUBLIC_URL="http://127.0.0.1/vault")
                os.environ.pop("SUBVAULT_ADMIN_PASSWORD", None)
                os.environ.pop("SUBVAULT_ADMIN_PASSWORD_B64", None)
                server = make_server("127.0.0.1", 0, App())
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                sub_port = server.server_port
                jar = http.cookiejar.CookieJar()
                browser = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

                def request(port, path, method="GET", payload=None, headers=None):
                    body = json.dumps(payload).encode() if payload is not None else None
                    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body,
                                                 method=method, headers={"Content-Type": "application/json", **(headers or {})})
                    with browser.open(req, timeout=3) as result:
                        return json.loads(result.read())

                with self.assertRaises(urllib.error.HTTPError) as denied:
                    request(sub_port, "/api/nodes")
                self.assertEqual(denied.exception.code, 401)
                with self.assertRaises(urllib.error.HTTPError) as no_direct_login:
                    request(sub_port, "/api/login", "POST", {"username": "admin", "password": "x"})
                self.assertEqual(no_direct_login.exception.code, 404)
                login = request(ng_port, "/api/auth/login", "POST", {
                    "username": "admin", "password": "test-gate-password-123",
                })
                csrf = login["csrf"]
                with self.assertRaises(urllib.error.HTTPError) as exposed_bridge:
                    request(ng_port, "/api/internal/subvault/session")
                self.assertEqual(exposed_bridge.exception.code, 404)
                self.assertEqual(request(sub_port, "/api/session")["auth_mode"], "nexusgate")
                with browser.open(f"http://127.0.0.1:{sub_port}/", timeout=3) as shell:
                    self.assertIn(b"/vault/static/app.js", shell.read())
                self.assertEqual(request(sub_port, "/api/nodes")["items"], [])
                with self.assertRaises(urllib.error.HTTPError) as wrong_csrf:
                    request(sub_port, "/api/nodes", "POST", {"uris": []}, {"X-CSRF-Token": "wrong"})
                self.assertEqual(wrong_csrf.exception.code, 403)
                node = request(sub_port, "/api/nodes", "POST", {"uris": [
                    "vless://11111111-1111-1111-1111-111111111111@example.com:443#Node"
                ]}, {"X-CSRF-Token": csrf})
                sub = request(sub_port, "/api/subscriptions", "POST", {
                    "name": "test", "enabled": True, "node_ids": node["ids"],
                }, {"X-CSRF-Token": csrf})
                token = request(sub_port, "/api/subscriptions")["items"][0]["token"]
                self.assertEqual(sub["id"], 1)
                request(ng_port, "/api/auth/logout", "POST", {}, {"X-CSRF-Token": csrf})
                with self.assertRaises(urllib.error.HTTPError) as expired:
                    request(sub_port, "/api/nodes")
                self.assertEqual(expired.exception.code, 401)
                connection = http.client.HTTPConnection("127.0.0.1", sub_port, timeout=3)
                connection.request("GET", "/")
                redirect = connection.getresponse()
                self.assertEqual((redirect.status, redirect.getheader("Location")), (302, "/"))
                redirect.read()
                connection.request("GET", f"/s/{token}/base64")
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                response.read()
                connection.close()
            finally:
                if server:
                    server.shutdown()
                    server.server_close()
                if thread:
                    thread.join(timeout=2)
                os.environ.clear()
                os.environ.update(old_env)
                process.terminate()
                try:
                    process.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()


if __name__ == "__main__":
    unittest.main()
