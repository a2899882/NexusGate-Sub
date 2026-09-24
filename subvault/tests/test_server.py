import base64
import http.cookiejar
import http.client
import json
import os
import socket
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from panel.server import App, make_server
from panel.db import Database


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_env = os.environ.copy()
        os.environ.update(
            SUBVAULT_DATA_DIR=self.temp.name,
            SUBVAULT_ADMIN_USER="admin",
            SUBVAULT_ADMIN_PASSWORD="testing-password-123",
            SUBVAULT_PUBLIC_URL="http://127.0.0.1",
            SUBVAULT_COOKIE_SECURE="0",
            SUBVAULT_AUTH_MODE="standalone",
            SUBVAULT_USAGE_REPORT_KEY="report-test-key",
        )
        self.app = App()
        self.server = make_server("127.0.0.1", 0, self.app)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        login = self.request("/api/login", "POST", {"username": "admin", "password": "testing-password-123"})
        self.csrf = login["csrf"]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        os.environ.clear()
        os.environ.update(self.old_env)
        self.temp.cleanup()

    def request(self, path, method="GET", payload=None, headers=None, raw=False):
        body = json.dumps(payload).encode() if payload is not None else None
        all_headers = {"Content-Type": "application/json", **(headers or {})}
        # Caddy strips /vault before forwarding to the local service. A direct
        # handler test must forward the browser's path-scoped cookie itself.
        if self.jar:
            all_headers["Cookie"] = "; ".join(f"{item.name}={item.value}" for item in self.jar)
        if hasattr(self, "csrf") and method != "GET":
            all_headers["X-CSRF-Token"] = self.csrf
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=body, headers=all_headers, method=method)
        with self.opener.open(req, timeout=3) as response:
            data = response.read()
            return (data, response.headers) if raw else json.loads(data)

    def test_health(self):
        self.assertEqual(self.request("/healthz")["status"], "ok")

    def test_worker_limit_rejects_excess_requests_instead_of_spawning_threads(self):
        for _ in range(self.app.max_workers):
            self.server._slots.acquire()
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=3) as client:
                client.sendall(b"GET /healthz HTTP/1.1\r\nHost: localhost\r\n\r\n")
                self.assertIn(b"503 Service Unavailable", client.recv(256))
        finally:
            for _ in range(self.app.max_workers):
                self.server._slots.release()

    def test_node_picker_options_page_without_leaking_links(self):
        with self.app.db.connect() as conn:
            now = "2026-09-24T00:00:00+00:00"
            conn.executemany(
                "INSERT INTO nodes(name,uri,group_name,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                ((f"节点 {index}", "ss://private-credential", "测试组", 1, now, now) for index in range(1001)),
            )
            conn.commit()
        first = self.request("/api/nodes/options?page=1&page_size=1000")
        second = self.request("/api/nodes/options?page=2&page_size=1000")
        self.assertEqual((first["total"], first["pages"], len(first["items"]), len(second["items"])), (1001, 2, 1000, 1))
        self.assertEqual(set(second["items"][0]), {"id", "name", "group_name", "enabled"})

    def test_account_name_and_password_change_revoke_old_session(self):
        self.request("/api/change-account", "POST", {
            "username": "new-owner", "current_password": "testing-password-123",
            "new_password": "new-testing-password-123", "confirm_password": "new-testing-password-123",
        })
        with self.assertRaises(urllib.error.HTTPError) as denied:
            self.request("/api/nodes")
        self.assertEqual(denied.exception.code, 401)
        with self.assertRaises(urllib.error.HTTPError) as old_login:
            self.request("/api/login", "POST", {"username": "admin", "password": "testing-password-123"})
        self.assertEqual(old_login.exception.code, 401)
        new_login = self.request("/api/login", "POST", {
            "username": "new-owner", "password": "new-testing-password-123",
        })
        self.assertEqual(new_login["username"], "new-owner")

    def test_existing_database_adds_subscription_group_column(self):
        path = os.path.join(self.temp.name, "legacy.db")
        with sqlite3.connect(path) as conn:
            conn.execute(
                """CREATE TABLE subscriptions (
                   id INTEGER PRIMARY KEY, name TEXT NOT NULL, token TEXT NOT NULL UNIQUE,
                   enabled INTEGER NOT NULL DEFAULT 1, expires_at TEXT)"""
            )
            conn.execute(
                "INSERT INTO subscriptions(id,name,token,enabled,expires_at) VALUES(1,'旧订阅','legacy-token',1,NULL)"
            )
        database = Database(path)
        database.initialize("legacy-admin", "testing-password-123")
        with database.connect() as conn:
            row = conn.execute("SELECT group_name FROM subscriptions WHERE id=1").fetchone()
        self.assertEqual(row["group_name"], "默认")

    def test_public_login_shell_is_neutral_and_admin_shell_requires_session(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=3) as response:
            login_html = response.read().decode()
        self.assertIn("PRIVATE SERVICE CONSOLE", login_html)
        self.assertNotIn("节点", login_html)
        self.assertNotIn("订阅", login_html)
        admin_html, _ = self.request("/", raw=True)
        self.assertIn('data-page="nodes"'.encode(), admin_html)
        self.assertIn(b'/vault/static/app.js', admin_html)
        self.assertTrue(all(cookie.path == "/vault" for cookie in self.jar))
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(f"http://127.0.0.1:{self.port}/static/app.js", timeout=3)
        self.assertEqual(caught.exception.code, 401)

    def test_delete_body_is_consumed_before_next_request_on_same_connection(self):
        uri = "vless://11111111-1111-1111-1111-111111111111@example.com:443?security=tls#Delete"
        node_id = self.request("/api/nodes", "POST", {"uris": [uri]})["ids"][0]
        cookie = "; ".join(f"{item.name}={item.value}" for item in self.jar)
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        headers = {
            "Cookie": cookie,
            "Content-Type": "application/json",
            "Content-Length": "2",
            "X-CSRF-Token": self.csrf,
        }
        connection.request("DELETE", f"/api/nodes/{node_id}", body="{}", headers=headers)
        deleted = connection.getresponse()
        self.assertEqual(deleted.status, 200)
        deleted.read()
        connection.request("GET", "/api/nodes", headers={"Cookie": cookie})
        listed = connection.getresponse()
        self.assertEqual(listed.status, 200)
        self.assertEqual(json.loads(listed.read())["items"], [])
        connection.close()

    def test_authenticated_qr_endpoint(self):
        body, headers = self.request("/api/qr?data=https%3A%2F%2Fsub.example.com%2Fs%2Ftoken%2Fauto", raw=True)
        self.assertTrue(body.startswith(b"<svg"))
        self.assertIn("image/svg+xml", headers["Content-Type"])

    def test_node_search_pagination_and_bulk_management(self):
        first = self.request("/api/nodes", "POST", {
            "uris": ["vless://11111111-1111-1111-1111-111111111111@alpha.example:443#Alpha"],
            "group_name": "美国",
        })["ids"][0]
        second = self.request("/api/nodes", "POST", {
            "uris": ["trojan://secret@beta.example:443#Beta"],
            "group_name": "日本",
        })["ids"][0]
        result = self.request("/api/nodes?q=Alpha&page=1&page_size=1&status=enabled")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["id"], first)
        self.assertIn("日本", result["groups"])
        changed = self.request("/api/nodes/bulk", "POST", {"ids": [first, second], "action": "group", "group_name": "主力"})
        self.assertEqual(changed["affected"], 2)
        self.request("/api/nodes/bulk", "POST", {"ids": [second], "action": "disable"})
        disabled = self.request(f"/api/nodes?group={urllib.parse.quote('主力')}&status=disabled")
        self.assertEqual([item["id"] for item in disabled["items"]], [second])

    def test_subscription_group_filter_and_bulk_management(self):
        for name, group in (("Alice", "设计组"), ("Bob", "开发组")):
            self.request("/api/subscriptions", "POST", {"name": name, "group_name": group, "node_ids": []})
        result = self.request(f"/api/subscriptions?q=Ali&group={urllib.parse.quote('设计组')}&page_size=1")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["group_name"], "设计组")
        subscription_id = result["items"][0]["id"]
        self.request("/api/subscriptions/bulk", "POST", {"ids": [subscription_id], "action": "disable"})
        disabled = self.request("/api/subscriptions?status=disabled")
        self.assertEqual([item["id"] for item in disabled["items"]], [subscription_id])

    def test_cleanup_enforces_age_and_row_caps(self):
        self.app.max_access_log_rows = 2
        self.app.max_usage_report_rows = 2
        with self.app.db.connect() as conn:
            for index in range(4):
                conn.execute(
                    """INSERT INTO access_logs(subscription_id,happened_at,ip,device,user_agent,target,allowed,reason,response_bytes)
                       VALUES(NULL,?,?,?,?,?,?,?,0)""",
                    (f"2026-01-0{index + 1}T00:00:00+00:00", "127.0.0.1", "test", "test", "auto", 1, "ok"),
                )
                conn.execute(
                    """INSERT INTO usage_reports(subscription_id,happened_at,source,upload_delta,download_delta)
                       VALUES(NULL,?,?,1,1)""",
                    (f"2026-09-0{index + 1}T00:00:00+00:00", "test"),
                )
            conn.commit()
        self.app.log_retention_days = 3650
        self.app.usage_retention_days = 3650
        self.app.cleanup(force=True)
        with self.app.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM access_logs").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM usage_reports").fetchone()[0], 2)

    def test_crud_and_subscription_ip_limit(self):
        uri = "vless://11111111-1111-1111-1111-111111111111@example.com:443?security=tls#Test"
        created = self.request("/api/nodes", "POST", {"uris": [uri], "group_name": "测试"})
        node_id = created["ids"][0]
        self.request(
            "/api/subscriptions",
            "POST",
            {"name": "朋友 A", "node_ids": [node_id], "ip_limit": 1, "device_limit": 0, "access_window_hours": 24, "traffic_limit_bytes": 10_000_000},
        )
        sub = self.request("/api/subscriptions")["items"][0]
        path = urllib.parse.urlsplit(sub["links"][0]["url"]).path + "?template=" + str(sub["links"][0]["template_id"])
        body, headers = self.request(path, headers={"X-Real-IP": "1.1.1.1", "User-Agent": "TestClient"}, raw=True)
        self.assertIn(uri, base64.b64decode(body).decode())
        self.assertIn("total=10000000", headers["Subscription-Userinfo"])
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request(path, headers={"X-Real-IP": "2.2.2.2", "User-Agent": "TestClient"}, raw=True)
        self.assertEqual(caught.exception.code, 403)

    def test_auto_detects_clash_and_v2ray_alias_is_base64(self):
        uri = "trojan://secret@example.com:443?sni=example.com#Trojan"
        node_id = self.request("/api/nodes", "POST", {"uris": [uri]})["ids"][0]
        self.request("/api/subscriptions", "POST", {"name": "auto", "node_ids": [node_id]})
        sub = self.request("/api/subscriptions")["items"][0]
        auto_path = urllib.parse.urlsplit(next(link["url"] for link in sub["links"] if link["target"] == "auto")).path
        body, headers = self.request(auto_path, headers={"User-Agent": "ClashMeta/1.19"}, raw=True)
        self.assertIn(b"proxy-groups:", body)
        self.assertIn("自动选择".encode(), body)
        self.assertEqual(headers["X-SubVault-Target"], "clash")
        v2ray_path = urllib.parse.urlsplit(next(link["url"] for link in sub["links"] if link["target"] == "v2ray")).path
        body, headers = self.request(v2ray_path, headers={"User-Agent": "v2rayNG/1.10"}, raw=True)
        self.assertIn(uri, base64.b64decode(body).decode())
        self.assertEqual(headers["X-SubVault-Target"], "base64")

    def test_usage_report(self):
        uri = "trojan://secret@example.com:443?sni=example.com#Trojan"
        node_id = self.request("/api/nodes", "POST", {"uris": [uri]})["ids"][0]
        self.request("/api/subscriptions", "POST", {"name": "quota", "node_ids": [node_id], "traffic_limit_bytes": 300})
        token = self.request("/api/subscriptions")["items"][0]["token"]
        result = self.request(
            "/api/v1/usage",
            "POST",
            {"subscription_token": token, "upload_bytes": 100, "download_bytes": 250, "source": "test"},
            headers={"Authorization": "Bearer report-test-key"},
        )
        self.assertEqual(result["traffic_used_bytes"], 350)
        self.assertFalse(result["allowed"])


if __name__ == "__main__":
    unittest.main()
