import http.client
import http.cookiejar
import json
import os
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from panel.server import App, make_server


class SubpathTests(unittest.TestCase):
    def test_login_assets_and_public_subscription_behind_vault_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            old_env = os.environ.copy()
            os.environ.update(
                SUBVAULT_DATA_DIR=directory,
                SUBVAULT_ADMIN_PASSWORD="testing-password-123",
                SUBVAULT_COOKIE_SECURE="0",
            )
            backend = proxy = None
            try:
                app = App()
                backend = make_server("127.0.0.1", 0, app)
                backend_thread = threading.Thread(target=backend.serve_forever, daemon=True)
                backend_thread.start()

                class Proxy(BaseHTTPRequestHandler):
                    def do_GET(self):
                        self.forward()

                    def do_POST(self):
                        self.forward()

                    def forward(self):
                        if not self.path.startswith("/vault/"):
                            self.send_error(404)
                            return
                        connection = http.client.HTTPConnection("127.0.0.1", backend.server_port)
                        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                        path = self.path[len("/vault"):]
                        connection.request(self.command, path, body=body, headers=dict(self.headers))
                        response = connection.getresponse()
                        content = response.read()
                        self.send_response(response.status)
                        for key, value in response.getheaders():
                            if key.lower() not in {"connection", "transfer-encoding", "content-length", "server", "date"}:
                                self.send_header(key, value)
                        self.send_header("Content-Length", str(len(content)))
                        self.end_headers()
                        self.wfile.write(content)
                        connection.close()

                    def log_message(self, *_args):
                        pass

                proxy = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
                app.public_url = f"http://127.0.0.1:{proxy.server_port}/vault"
                proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
                proxy_thread.start()
                base = app.public_url
                jar = http.cookiejar.CookieJar()
                browser = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

                def request(path, data=None, csrf=None):
                    payload = None if data is None else json.dumps(data).encode()
                    headers = {"Content-Type": "application/json"}
                    if csrf:
                        headers["X-CSRF-Token"] = csrf
                    with browser.open(urllib.request.Request(base + path, data=payload, headers=headers), timeout=3) as result:
                        return result.read(), result.headers

                login_shell, _ = request("/")
                self.assertIn(b"/vault/static/login.js", login_shell)
                login, headers = request("/api/login", {"username": "admin", "password": "testing-password-123"})
                csrf = json.loads(login)["csrf"]
                self.assertIn("Path=/vault", headers["Set-Cookie"])
                shell, _ = request("/")
                self.assertIn(b"/vault/static/app.js", shell)
                javascript, _ = request("/static/app.js")
                self.assertIn(b"fetch(`/vault${path}`", javascript)
                session, _ = request("/api/session")
                self.assertEqual(json.loads(session)["username"], "admin")
                node, _ = request("/api/nodes", {"uris": [
                    "vless://11111111-1111-1111-1111-111111111111@example.com:443#Node"
                ]}, csrf)
                subscription, _ = request("/api/subscriptions", {
                    "name": "test", "enabled": True, "node_ids": json.loads(node)["ids"]
                }, csrf)
                self.assertGreater(json.loads(subscription)["id"], 0)
                listed, _ = request("/api/subscriptions")
                # Every public subscription URL keeps the /vault prefix.
                self.assertIn(base + "/s/", listed.decode())
            finally:
                if proxy:
                    proxy.shutdown()
                    proxy.server_close()
                if backend:
                    backend.shutdown()
                    backend.server_close()
                os.environ.clear()
                os.environ.update(old_env)
