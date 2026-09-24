import argparse
import base64
import http.client
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from . import __version__
from .db import Database, rows_to_dicts, utcnow
from .formats import SUPPORTED_SCHEMES, parse_node, render_subscription, safe_filename
from .qr import qr_svg
from .security import hash_password, random_token, stable_hash, token_hash, verify_password


STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
TARGET_EXTENSIONS = {"base64": "txt", "clash": "yaml", "singbox": "json", "surge": "conf"}


class HTTPError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(message)


def parse_bool(value, default=True) -> int:
    if value is None:
        return int(default)
    return int(bool(value))


def as_nonnegative_int(value, field: str, maximum=10**18) -> int:
    try:
        result = int(value or 0)
    except (TypeError, ValueError):
        raise HTTPError(400, f"{field} 必须是整数")
    if result < 0 or result > maximum:
        raise HTTPError(400, f"{field} 超出允许范围")
    return result


def validate_expiry(value):
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")
    except ValueError:
        raise HTTPError(400, "到期时间格式无效")


def env_int(name, default, minimum=1, maximum=10**9):
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


def query_value(query, name, default="", maximum=160):
    return str(query.get(name, [default])[0]).strip()[:maximum]


def pagination(query, default_size=1000):
    try:
        page = max(1, int(query_value(query, "page", "1", 12)))
        page_size = max(1, min(1000, int(query_value(query, "page_size", str(default_size), 12))))
    except ValueError:
        raise HTTPError(400, "分页参数无效")
    return page, page_size, (page - 1) * page_size


class App:
    def __init__(self):
        data_dir = Path(os.getenv("SUBVAULT_DATA_DIR", "/data"))
        data_dir.mkdir(parents=True, exist_ok=True)
        self.db = Database(str(data_dir / "subvault.db"))
        self.auth_mode = os.getenv("SUBVAULT_AUTH_MODE", "standalone")
        if self.auth_mode not in {"standalone", "nexusgate"}:
            raise RuntimeError("SUBVAULT_AUTH_MODE 无效")
        admin_user = os.getenv("SUBVAULT_ADMIN_USER", "admin")
        admin_password = os.getenv("SUBVAULT_ADMIN_PASSWORD", "")
        if os.getenv("SUBVAULT_ADMIN_PASSWORD_B64"):
            try:
                admin_password = base64.b64decode(os.environ["SUBVAULT_ADMIN_PASSWORD_B64"]).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                raise RuntimeError("SUBVAULT_ADMIN_PASSWORD_B64 格式无效")
        if not admin_password:
            if self.auth_mode == "standalone":
                raise RuntimeError("必须设置 SUBVAULT_ADMIN_PASSWORD")
            admin_password = random_token(32)  # Only used if the old database has no administrator.
        self.db.initialize(admin_user, admin_password)
        self.bridge_key = os.getenv("SUBVAULT_BRIDGE_KEY", "")
        if self.auth_mode == "nexusgate" and not self.bridge_key:
            raise RuntimeError("联合登录缺少桥接密钥")
        self.public_url = os.getenv("SUBVAULT_PUBLIC_URL", "").rstrip("/")
        self.secure_cookie = os.getenv("SUBVAULT_COOKIE_SECURE", "1") != "0"
        self.session_hours = max(1, int(os.getenv("SUBVAULT_SESSION_HOURS", "24")))
        self.report_key = os.getenv("SUBVAULT_USAGE_REPORT_KEY", "")
        self.log_retention_days = env_int("SUBVAULT_LOG_RETENTION_DAYS", 30, 1, 3650)
        self.usage_retention_days = env_int("SUBVAULT_USAGE_RETENTION_DAYS", 180, 1, 3650)
        self.access_retention_days = env_int("SUBVAULT_ACCESS_RETENTION_DAYS", 90, 1, 3650)
        self.max_access_log_rows = env_int("SUBVAULT_MAX_ACCESS_LOG_ROWS", 100000, 1000, 10000000)
        self.max_usage_report_rows = env_int("SUBVAULT_MAX_USAGE_REPORT_ROWS", 100000, 1000, 10000000)
        self.cleanup_interval = env_int("SUBVAULT_CLEANUP_INTERVAL_SECONDS", 21600, 300, 604800)
        self.max_workers = env_int("SUBVAULT_MAX_WORKERS", 24, 4, 64)
        self.login_attempts = {}
        self.login_lock = threading.Lock()
        self.cleanup_lock = threading.Lock()
        self.last_cleanup = 0.0
        self.last_cleanup_at = None

    def cleanup(self, force=True):
        now_monotonic = time.monotonic()
        if not force and now_monotonic - self.last_cleanup < self.cleanup_interval:
            return False
        if not self.cleanup_lock.acquire(blocking=False):
            return False
        try:
            now_monotonic = time.monotonic()
            if not force and now_monotonic - self.last_cleanup < self.cleanup_interval:
                return False
            now_dt = datetime.now(timezone.utc)
            log_cutoff = (now_dt - timedelta(days=self.log_retention_days)).isoformat(timespec="seconds")
            usage_cutoff = (now_dt - timedelta(days=self.usage_retention_days)).isoformat(timespec="seconds")
            access_cutoff = (now_dt - timedelta(days=self.access_retention_days)).isoformat(timespec="seconds")
            with self.db.connect() as conn:
                conn.execute("DELETE FROM sessions WHERE expires_at < ?", (utcnow(),))
                conn.execute("DELETE FROM access_logs WHERE happened_at < ?", (log_cutoff,))
                conn.execute("DELETE FROM usage_reports WHERE happened_at < ?", (usage_cutoff,))
                conn.execute("DELETE FROM access_keys WHERE last_seen_at < ?", (access_cutoff,))
                conn.execute(
                    """DELETE FROM access_logs WHERE id < COALESCE(
                       (SELECT MIN(id) FROM (SELECT id FROM access_logs ORDER BY id DESC LIMIT ?)),0)""",
                    (self.max_access_log_rows,),
                )
                conn.execute(
                    """DELETE FROM usage_reports WHERE id < COALESCE(
                       (SELECT MIN(id) FROM (SELECT id FROM usage_reports ORDER BY id DESC LIMIT ?)),0)""",
                    (self.max_usage_report_rows,),
                )
                conn.execute("PRAGMA optimize")
                conn.commit()
            with self.db.connect() as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.last_cleanup = time.monotonic()
            self.last_cleanup_at = utcnow()
            return True
        finally:
            self.cleanup_lock.release()


class Handler(BaseHTTPRequestHandler):
    server_version = "SubVault"
    # Caddy is the only upstream; closing after each response keeps idle
    # keep-alive sockets from occupying the bounded request worker pool.
    protocol_version = "HTTP/1.0"
    app: App

    def log_message(self, fmt, *args):
        if args:
            request = re.sub(r"(/s/)[^/\s?]+", r"\1[REDACTED]", str(args[0]))
            request = re.sub(r"(/api/qr)\?[^\s]+", r"\1?[REDACTED]", request)
            if " /healthz " in request:
                return
            args = (request, *args[1:])
        print(f"{self.address_string()} - {fmt % args}")

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, method: str):
        try:
            self._body_cache = None
            if method in ("POST", "PUT", "DELETE"):
                self._body_cache = self._read_request_body()
            split = urlsplit(self.path)
            path = split.path
            query = parse_qs(split.query)
            if path == "/healthz" and method == "GET":
                return self.send_json({"status": "ok", "version": __version__})
            if path == "/api/login" and method == "POST":
                if self.app.auth_mode == "nexusgate":
                    raise HTTPError(404, "请从 NexusGate 登录")
                return self.login()
            if path.startswith("/s/") and method == "GET":
                return self.serve_subscription(path, query)
            if path == "/api/v1/usage" and method == "POST":
                return self.report_usage()
            if path.startswith("/api/"):
                session = self.require_session(mutating=method in ("POST", "PUT", "DELETE"))
                return self.route_api(method, path, query, session)
            if method == "GET":
                if path.startswith("/static/"):
                    return self.serve_static(path)
                return self.serve_app_shell()
            raise HTTPError(404, "页面不存在")
        except HTTPError as exc:
            self.send_json({"error": exc.message}, exc.status)
        except BrokenPipeError:
            return
        except Exception as exc:
            print(f"Unhandled error: {type(exc).__name__}: {exc}")
            self.send_json({"error": "服务器内部错误"}, 500)

    def route_api(self, method, path, query, session):
        if path == "/api/session" and method == "GET":
            return self.send_json({"username": session["username"], "csrf": session["csrf_token"], "auth_mode": self.app.auth_mode})
        if path == "/api/qr" and method == "GET":
            return self.serve_qr(query)
        if path == "/api/logout" and method == "POST":
            if self.app.auth_mode == "nexusgate":
                raise HTTPError(404, "请从 NexusGate 退出")
            return self.logout()
        if path == "/api/dashboard" and method == "GET":
            return self.dashboard()
        if path == "/api/nodes":
            if method == "GET":
                return self.list_nodes(query)
            if method == "POST":
                return self.create_nodes()
        if path == "/api/nodes/options" and method == "GET":
            return self.node_options(query)
        if path == "/api/nodes/bulk" and method == "POST":
            return self.bulk_nodes()
        match = re.fullmatch(r"/api/nodes/(\d+)", path)
        if match:
            if method == "PUT":
                return self.update_node(int(match.group(1)))
            if method == "DELETE":
                return self.delete_node(int(match.group(1)))
        if path == "/api/templates":
            if method == "GET":
                return self.list_templates()
            if method == "POST":
                return self.create_template()
        match = re.fullmatch(r"/api/templates/(\d+)", path)
        if match:
            if method == "PUT":
                return self.update_template(int(match.group(1)))
            if method == "DELETE":
                return self.delete_template(int(match.group(1)))
        if path == "/api/subscriptions":
            if method == "GET":
                return self.list_subscriptions(query)
            if method == "POST":
                return self.create_subscription()
        if path == "/api/subscriptions/bulk" and method == "POST":
            return self.bulk_subscriptions()
        match = re.fullmatch(r"/api/subscriptions/(\d+)", path)
        if match:
            if method == "PUT":
                return self.update_subscription(int(match.group(1)))
            if method == "DELETE":
                return self.delete_subscription(int(match.group(1)))
        match = re.fullmatch(r"/api/subscriptions/(\d+)/(rotate|reset-traffic|clear-access)", path)
        if match and method == "POST":
            return self.subscription_action(int(match.group(1)), match.group(2))
        match = re.fullmatch(r"/api/subscriptions/(\d+)/access", path)
        if match and method == "GET":
            return self.subscription_access(int(match.group(1)))
        if path == "/api/logs" and method == "GET":
            return self.list_logs(query)
        if path == "/api/settings" and method == "GET":
            return self.settings()
        if path == "/api/change-password" and method == "POST":
            if self.app.auth_mode == "nexusgate":
                raise HTTPError(404, "请在 NexusGate 修改账号")
            return self.change_password(session)
        if path == "/api/change-account" and method == "POST":
            if self.app.auth_mode == "nexusgate":
                raise HTTPError(404, "请在 NexusGate 修改账号")
            return self.change_account(session)
        raise HTTPError(404, "接口不存在")

    def read_json(self):
        body = self._body_cache if self._body_cache is not None else self._read_request_body()
        if not body:
            raise HTTPError(400, "请求内容为空")
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise HTTPError(400, "JSON 格式无效")

    def _read_request_body(self):
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            raise HTTPError(400, "不支持分块请求体")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.close_connection = True
            raise HTTPError(400, "请求长度无效")
        if length < 0 or length > 1024 * 1024:
            self.close_connection = True
            raise HTTPError(400, "请求内容过大")
        return self.rfile.read(length) if length else b""

    def client_ip(self):
        return (self.headers.get("X-Real-IP") or self.client_address[0]).split(",", 1)[0].strip()[:128]

    def send_json(self, payload, status=200, headers=None):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.security_headers()
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def send_bytes(self, body: bytes, content_type: str, status=200, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.security_headers()
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'self'")

    def serve_static(self, path):
        requested = (STATIC_DIR / path.removeprefix("/static/")).resolve()
        if STATIC_DIR.resolve() not in requested.parents:
            raise HTTPError(404, "文件不存在")
        if requested.name in {"app.js", "index.html"} and not self.session_or_none():
            raise HTTPError(401, "请先登录")
        if not requested.is_file():
            raise HTTPError(404, "文件不存在")
        content_type = mimetypes.guess_type(str(requested))[0] or "application/octet-stream"
        cache = "no-cache" if path.startswith("/static/") else "no-store"
        self.send_bytes(requested.read_bytes(), content_type, headers={"Cache-Control": cache})

    def serve_app_shell(self):
        session = self.session_or_none()
        if self.app.auth_mode == "nexusgate" and not session:
            self.send_response(302)
            self.send_header("Location", "/")
            self.send_header("Content-Length", "0")
            self.security_headers()
            self.end_headers()
            return
        requested = STATIC_DIR / ("index.html" if session else "login.html")
        self.send_bytes(requested.read_bytes(), "text/html; charset=utf-8", headers={"Cache-Control": "no-store"})

    def serve_qr(self, query):
        value = query.get("data", [""])[0]
        title = query.get("title", ["订阅二维码"])[0][:120]
        if not value:
            raise HTTPError(400, "二维码内容不能为空")
        try:
            body = qr_svg(value, title)
        except ValueError as exc:
            raise HTTPError(400, str(exc))
        self.send_bytes(body, "image/svg+xml; charset=utf-8", headers={"Cache-Control": "no-store"})

    def cookies(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return {}
        return {key: morsel.value for key, morsel in cookie.items()}

    def session_or_none(self):
        if self.app.auth_mode == "nexusgate":
            cookie = self.headers.get("Cookie", "")
            if not cookie or len(cookie) > 8192:
                return None
            connection = http.client.HTTPConnection("127.0.0.1", env_int("SUBVAULT_NG_PORT", 8787, 1, 65535), timeout=2)
            try:
                connection.request("GET", "/api/internal/subvault/session", headers={
                    "Cookie": cookie, "X-NG-Bridge-Key": self.app.bridge_key,
                })
                response = connection.getresponse()
                data = json.loads(response.read(4096)) if response.status == 200 else {}
                if data.get("authenticated") and data.get("username") and data.get("csrf"):
                    return {"username": data["username"], "csrf_token": data["csrf"]}
            except (OSError, ValueError, http.client.HTTPException):
                return None
            finally:
                connection.close()
            return None
        raw = self.cookies().get("subvault_session", "")
        if not raw:
            return None
        with self.app.db.connect() as conn:
            row = conn.execute(
                """SELECT sessions.*, admins.username FROM sessions JOIN admins ON admins.id=sessions.admin_id
                   WHERE sessions.token_hash=? AND sessions.expires_at>?""",
                (token_hash(raw), utcnow()),
            ).fetchone()
        return dict(row) if row else None

    def require_session(self, mutating=False):
        row = self.session_or_none()
        if not row:
            raise HTTPError(401, "登录已过期")
        if mutating and self.headers.get("X-CSRF-Token", "") != row["csrf_token"]:
            raise HTTPError(403, "安全令牌无效，请刷新页面")
        return row

    def _login_allowed(self, ip):
        now = time.monotonic()
        with self.app.login_lock:
            attempts = [stamp for stamp in self.app.login_attempts.get(ip, []) if now - stamp < 300]
            self.app.login_attempts[ip] = attempts
            return len(attempts) < 8

    def login(self):
        ip = self.client_ip()
        if not self._login_allowed(ip):
            raise HTTPError(429, "登录失败次数过多，请 5 分钟后再试")
        data = self.read_json()
        username = str(data.get("username", ""))[:128]
        password = str(data.get("password", ""))
        with self.app.db.connect() as conn:
            admin = conn.execute("SELECT * FROM admins WHERE username=?", (username,)).fetchone()
        if not admin or not verify_password(password, admin["password_hash"]):
            with self.app.login_lock:
                self.app.login_attempts.setdefault(ip, []).append(time.monotonic())
            time.sleep(0.25)
            raise HTTPError(401, "用户名或密码错误")
        raw, csrf = random_token(36), random_token(24)
        expires = (datetime.now(timezone.utc) + timedelta(hours=self.app.session_hours)).isoformat(timespec="seconds")
        with self.app.db.connect() as conn:
            conn.execute(
                "INSERT INTO sessions(token_hash,admin_id,csrf_token,expires_at,created_at) VALUES(?,?,?,?,?)",
                (token_hash(raw), admin["id"], csrf, expires, utcnow()),
            )
            conn.commit()
        cookie = f"subvault_session={raw}; Path=/vault; HttpOnly; SameSite=Strict; Max-Age={self.app.session_hours * 3600}"
        if self.app.secure_cookie:
            cookie += "; Secure"
        self.send_json({"username": username, "csrf": csrf}, headers={"Set-Cookie": cookie})

    def logout(self):
        raw = self.cookies().get("subvault_session", "")
        if raw:
            with self.app.db.connect() as conn:
                conn.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash(raw),))
                conn.commit()
        self.send_json({"ok": True}, headers={"Set-Cookie": "subvault_session=; Path=/vault; HttpOnly; SameSite=Strict; Max-Age=0"})

    def dashboard(self):
        with self.app.db.connect() as conn:
            totals = {
                "nodes": conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0],
                "subscriptions": conn.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0],
                "active": conn.execute("SELECT COUNT(*) FROM subscriptions WHERE enabled=1 AND (expires_at IS NULL OR expires_at>?)", (utcnow(),)).fetchone()[0],
                "blocked_24h": conn.execute("SELECT COUNT(*) FROM access_logs WHERE allowed=0 AND happened_at>?", ((datetime.now(timezone.utc)-timedelta(hours=24)).isoformat(timespec="seconds"),)).fetchone()[0],
                "traffic_used": conn.execute("SELECT COALESCE(SUM(traffic_used_bytes),0) FROM subscriptions").fetchone()[0],
            }
            recent = rows_to_dicts(conn.execute(
                """SELECT l.*,s.name subscription_name FROM access_logs l LEFT JOIN subscriptions s ON s.id=l.subscription_id
                   ORDER BY l.id DESC LIMIT 8"""
            ).fetchall())
        self.send_json({"totals": totals, "recent": recent})

    def list_nodes(self, query):
        page, page_size, offset = pagination(query)
        search = query_value(query, "q")
        group = query_value(query, "group", maximum=80)
        status = query_value(query, "status", "all", 20)
        where, params = [], []
        if search:
            where.append("(name LIKE ? OR group_name LIKE ? OR uri LIKE ?)")
            pattern = f"%{search}%"
            params.extend((pattern, pattern, pattern))
        if group:
            where.append("group_name=?")
            params.append(group)
        if status == "enabled":
            where.append("enabled=1")
        elif status == "disabled":
            where.append("enabled=0")
        elif status != "all":
            raise HTTPError(400, "节点状态筛选无效")
        clause = " WHERE " + " AND ".join(where) if where else ""
        with self.app.db.connect() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM nodes{clause}", params).fetchone()[0]
            rows = rows_to_dicts(conn.execute(
                f"SELECT * FROM nodes{clause} ORDER BY id DESC LIMIT ? OFFSET ?",
                (*params, page_size, offset),
            ).fetchall())
            groups = [row[0] for row in conn.execute("SELECT DISTINCT group_name FROM nodes ORDER BY group_name").fetchall()]
        self.send_json({
            "items": rows, "groups": groups, "total": total, "page": page, "page_size": page_size,
            "pages": max(1, (total + page_size - 1) // page_size),
        })

    def node_options(self, query):
        """Return only picker labels, never all node credentials for a subscription form."""
        page, page_size, offset = pagination(query)
        with self.app.db.connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
            rows = rows_to_dicts(conn.execute(
                "SELECT id,name,group_name,enabled FROM nodes ORDER BY id LIMIT ? OFFSET ?",
                (page_size, offset),
            ).fetchall())
        self.send_json({"items": rows, "total": total, "page": page,
                        "pages": max(1, (total + page_size - 1) // page_size)})

    def create_nodes(self):
        data = self.read_json()
        values = data.get("uris") if isinstance(data.get("uris"), list) else [data.get("uri")]
        values = [str(item).strip() for item in values if str(item or "").strip()]
        if not values or len(values) > 200:
            raise HTTPError(400, "请输入 1 至 200 条节点链接")
        group = str(data.get("group_name") or "默认").strip()[:80]
        custom_name = str(data.get("name") or "").strip()[:120]
        created = []
        with self.app.db.connect() as conn:
            now = utcnow()
            for index, uri in enumerate(values, 1):
                if len(uri) > 8192:
                    raise HTTPError(400, "节点链接过长")
                scheme = uri.split(":", 1)[0].lower() if ":" in uri else ""
                if scheme not in SUPPORTED_SCHEMES:
                    raise HTTPError(400, f"暂不支持 {scheme or '未知'} 节点协议")
                try:
                    parsed_name = parse_node(uri, f"{scheme.upper()} 节点 {index}")["name"]
                except Exception:
                    parsed_name = f"{scheme.upper()} 节点 {index}"
                name = custom_name if len(values) == 1 and custom_name else parsed_name
                cursor = conn.execute(
                    "INSERT INTO nodes(name,uri,group_name,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                    (name[:120], uri, group, parse_bool(data.get("enabled")), now, now),
                )
                created.append(cursor.lastrowid)
            conn.commit()
        self.send_json({"ok": True, "ids": created}, 201)

    def update_node(self, node_id):
        data = self.read_json()
        name = str(data.get("name", "")).strip()[:120]
        uri = str(data.get("uri", "")).strip()
        group = str(data.get("group_name") or "默认").strip()[:80]
        if not name or not uri:
            raise HTTPError(400, "节点名称和链接不能为空")
        scheme = uri.split(":", 1)[0].lower() if ":" in uri else ""
        if scheme not in SUPPORTED_SCHEMES:
            raise HTTPError(400, "节点协议不受支持")
        with self.app.db.connect() as conn:
            cursor = conn.execute(
                "UPDATE nodes SET name=?,uri=?,group_name=?,enabled=?,updated_at=? WHERE id=?",
                (name, uri, group, parse_bool(data.get("enabled")), utcnow(), node_id),
            )
            conn.commit()
        if not cursor.rowcount:
            raise HTTPError(404, "节点不存在")
        self.send_json({"ok": True})

    def delete_node(self, node_id):
        with self.app.db.connect() as conn:
            cursor = conn.execute("DELETE FROM nodes WHERE id=?", (node_id,))
            conn.commit()
        if not cursor.rowcount:
            raise HTTPError(404, "节点不存在")
        self.send_json({"ok": True})

    @staticmethod
    def _bulk_fields(data):
        ids = data.get("ids", [])
        if not isinstance(ids, list):
            raise HTTPError(400, "编号列表无效")
        try:
            ids = sorted(set(int(item) for item in ids))
        except (TypeError, ValueError):
            raise HTTPError(400, "编号列表无效")
        if not ids or len(ids) > 500:
            raise HTTPError(400, "请选择 1 至 500 条记录")
        return ids, str(data.get("action", "")).strip().lower()

    def bulk_nodes(self):
        data = self.read_json()
        ids, action = self._bulk_fields(data)
        placeholders = ",".join("?" for _ in ids)
        params = list(ids)
        with self.app.db.connect() as conn:
            if action == "delete":
                cursor = conn.execute(f"DELETE FROM nodes WHERE id IN ({placeholders})", params)
            elif action in ("enable", "disable"):
                cursor = conn.execute(
                    f"UPDATE nodes SET enabled=?,updated_at=? WHERE id IN ({placeholders})",
                    (int(action == "enable"), utcnow(), *params),
                )
            elif action == "group":
                group = str(data.get("group_name") or "").strip()[:80]
                if not group:
                    raise HTTPError(400, "请输入目标分组")
                cursor = conn.execute(
                    f"UPDATE nodes SET group_name=?,updated_at=? WHERE id IN ({placeholders})",
                    (group, utcnow(), *params),
                )
            else:
                raise HTTPError(400, "批量操作无效")
            conn.commit()
        self.send_json({"ok": True, "affected": cursor.rowcount})

    def list_templates(self):
        with self.app.db.connect() as conn:
            rows = rows_to_dicts(conn.execute("SELECT * FROM templates ORDER BY target,id").fetchall())
        self.send_json({"items": rows})

    def _template_fields(self, data):
        name = str(data.get("name", "")).strip()[:120]
        target = str(data.get("target", "")).strip().lower()
        content = str(data.get("content", ""))
        if not name or target not in TARGET_EXTENSIONS:
            raise HTTPError(400, "模板名称或类型无效")
        if target != "base64" and len(content) < 10:
            raise HTTPError(400, "模板内容过短")
        if len(content) > 256 * 1024:
            raise HTTPError(400, "模板内容不能超过 256KB")
        return name, target, content

    def create_template(self):
        name, target, content = self._template_fields(self.read_json())
        try:
            with self.app.db.connect() as conn:
                now = utcnow()
                cursor = conn.execute(
                    "INSERT INTO templates(name,target,content,builtin,created_at,updated_at) VALUES(?,?,?,0,?,?)",
                    (name, target, content, now, now),
                )
                conn.commit()
            self.send_json({"ok": True, "id": cursor.lastrowid}, 201)
        except sqlite3.IntegrityError:
            raise HTTPError(409, "模板名称已存在")

    def update_template(self, template_id):
        name, target, content = self._template_fields(self.read_json())
        try:
            with self.app.db.connect() as conn:
                cursor = conn.execute(
                    "UPDATE templates SET name=?,target=?,content=?,updated_at=? WHERE id=?",
                    (name, target, content, utcnow(), template_id),
                )
                conn.commit()
            if not cursor.rowcount:
                raise HTTPError(404, "模板不存在")
            self.send_json({"ok": True})
        except sqlite3.IntegrityError:
            raise HTTPError(409, "模板名称已存在")

    def delete_template(self, template_id):
        with self.app.db.connect() as conn:
            row = conn.execute("SELECT builtin FROM templates WHERE id=?", (template_id,)).fetchone()
            if not row:
                raise HTTPError(404, "模板不存在")
            if row["builtin"]:
                raise HTTPError(400, "内置模板可以编辑但不能删除")
            conn.execute("DELETE FROM templates WHERE id=?", (template_id,))
            conn.commit()
        self.send_json({"ok": True})

    def _subscription_fields(self, data):
        name = str(data.get("name", "")).strip()[:120]
        if not name:
            raise HTTPError(400, "订阅名称不能为空")
        node_ids = data.get("node_ids", [])
        if not isinstance(node_ids, list):
            raise HTTPError(400, "节点列表无效")
        try:
            node_ids = sorted(set(int(item) for item in node_ids))
        except (TypeError, ValueError):
            raise HTTPError(400, "节点编号无效")
        return {
            "name": name,
            "group_name": str(data.get("group_name") or "默认").strip()[:80] or "默认",
            "enabled": parse_bool(data.get("enabled")),
            "expires_at": validate_expiry(data.get("expires_at")),
            "traffic_limit_bytes": as_nonnegative_int(data.get("traffic_limit_bytes"), "流量上限"),
            "ip_limit": as_nonnegative_int(data.get("ip_limit"), "IP 上限", 10000),
            "device_limit": as_nonnegative_int(data.get("device_limit"), "设备上限", 10000),
            "access_window_hours": max(1, min(720, as_nonnegative_int(data.get("access_window_hours", 24), "活跃窗口", 720))),
            "notes": str(data.get("notes", ""))[:2000],
            "node_ids": node_ids,
        }

    @staticmethod
    def _set_subscription_nodes(conn, sub_id, node_ids):
        conn.execute("DELETE FROM subscription_nodes WHERE subscription_id=?", (sub_id,))
        for node_id in node_ids:
            exists = conn.execute("SELECT 1 FROM nodes WHERE id=?", (node_id,)).fetchone()
            if not exists:
                raise HTTPError(400, f"节点 {node_id} 不存在")
            conn.execute("INSERT INTO subscription_nodes(subscription_id,node_id) VALUES(?,?)", (sub_id, node_id))

    def create_subscription(self):
        fields = self._subscription_fields(self.read_json())
        now = utcnow()
        with self.app.db.transaction() as conn:
            cursor = conn.execute(
                """INSERT INTO subscriptions(name,group_name,token,enabled,expires_at,traffic_limit_bytes,ip_limit,device_limit,
                   access_window_hours,notes,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (fields["name"], fields["group_name"], random_token(24), fields["enabled"], fields["expires_at"], fields["traffic_limit_bytes"],
                 fields["ip_limit"], fields["device_limit"], fields["access_window_hours"], fields["notes"], now, now),
            )
            self._set_subscription_nodes(conn, cursor.lastrowid, fields["node_ids"])
        self.send_json({"ok": True, "id": cursor.lastrowid}, 201)

    def update_subscription(self, sub_id):
        fields = self._subscription_fields(self.read_json())
        with self.app.db.transaction() as conn:
            cursor = conn.execute(
                """UPDATE subscriptions SET name=?,group_name=?,enabled=?,expires_at=?,traffic_limit_bytes=?,ip_limit=?,device_limit=?,
                   access_window_hours=?,notes=?,updated_at=? WHERE id=?""",
                (fields["name"], fields["group_name"], fields["enabled"], fields["expires_at"], fields["traffic_limit_bytes"], fields["ip_limit"],
                 fields["device_limit"], fields["access_window_hours"], fields["notes"], utcnow(), sub_id),
            )
            if not cursor.rowcount:
                raise HTTPError(404, "订阅不存在")
            self._set_subscription_nodes(conn, sub_id, fields["node_ids"])
        self.send_json({"ok": True})

    def list_subscriptions(self, query):
        page, page_size, offset = pagination(query)
        search = query_value(query, "q")
        group = query_value(query, "group", maximum=80)
        status = query_value(query, "status", "all", 20)
        where, params = [], []
        now = utcnow()
        if search:
            where.append("(name LIKE ? OR group_name LIKE ? OR notes LIKE ?)")
            pattern = f"%{search}%"
            params.extend((pattern, pattern, pattern))
        if group:
            where.append("group_name=?")
            params.append(group)
        if status == "active":
            where.append("enabled=1 AND (expires_at IS NULL OR expires_at>?) AND (traffic_limit_bytes=0 OR traffic_used_bytes<traffic_limit_bytes)")
            params.append(now)
        elif status == "disabled":
            where.append("enabled=0")
        elif status == "expired":
            where.append("expires_at IS NOT NULL AND expires_at<=?")
            params.append(now)
        elif status == "exhausted":
            where.append("traffic_limit_bytes>0 AND traffic_used_bytes>=traffic_limit_bytes")
        elif status != "all":
            raise HTTPError(400, "订阅状态筛选无效")
        clause = " WHERE " + " AND ".join(f"({item})" for item in where) if where else ""
        with self.app.db.connect() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM subscriptions{clause}", params).fetchone()[0]
            rows = rows_to_dicts(conn.execute(
                f"SELECT * FROM subscriptions{clause} ORDER BY id DESC LIMIT ? OFFSET ?",
                (*params, page_size, offset),
            ).fetchall())
            templates = rows_to_dicts(conn.execute("SELECT id,name,target FROM templates ORDER BY target,id").fetchall())
            node_map = {row["id"]: [] for row in rows}
            if node_map:
                placeholders = ",".join("?" for _ in node_map)
                for sub_id, node_id in conn.execute(
                    f"SELECT subscription_id,node_id FROM subscription_nodes WHERE subscription_id IN ({placeholders}) ORDER BY node_id",
                    tuple(node_map),
                ).fetchall():
                    node_map[sub_id].append(node_id)
            for row in rows:
                row["node_ids"] = node_map[row["id"]]
                row["links"] = self.make_links(row["token"], templates)
            groups = [row[0] for row in conn.execute("SELECT DISTINCT group_name FROM subscriptions ORDER BY group_name").fetchall()]
        self.send_json({
            "items": rows, "templates": templates, "groups": groups, "total": total, "page": page,
            "page_size": page_size, "pages": max(1, (total + page_size - 1) // page_size),
        })

    def make_links(self, token, templates):
        base = self.app.public_url
        links = [
            {"template_id": None, "name": "自动识别客户端", "target": "auto", "url": f"{base}/s/{quote(token)}/auto"},
            {"template_id": None, "name": "V2Ray / v2rayNG", "target": "v2ray", "url": f"{base}/s/{quote(token)}/v2ray"},
            {"template_id": None, "name": "Shadowrocket 小火箭", "target": "shadowrocket", "url": f"{base}/s/{quote(token)}/shadowrocket"},
        ]
        links.extend([
            {
                "template_id": item["id"], "name": item["name"], "target": item["target"],
                "url": f"{base}/s/{quote(token)}/{item['target']}?template={item['id']}",
            }
            for item in templates
        ])
        return links

    def delete_subscription(self, sub_id):
        with self.app.db.connect() as conn:
            cursor = conn.execute("DELETE FROM subscriptions WHERE id=?", (sub_id,))
            conn.commit()
        if not cursor.rowcount:
            raise HTTPError(404, "订阅不存在")
        self.send_json({"ok": True})

    def bulk_subscriptions(self):
        data = self.read_json()
        ids, action = self._bulk_fields(data)
        placeholders = ",".join("?" for _ in ids)
        params = list(ids)
        with self.app.db.connect() as conn:
            if action == "delete":
                cursor = conn.execute(f"DELETE FROM subscriptions WHERE id IN ({placeholders})", params)
            elif action in ("enable", "disable"):
                cursor = conn.execute(
                    f"UPDATE subscriptions SET enabled=?,updated_at=? WHERE id IN ({placeholders})",
                    (int(action == "enable"), utcnow(), *params),
                )
            elif action == "group":
                group = str(data.get("group_name") or "").strip()[:80]
                if not group:
                    raise HTTPError(400, "请输入目标分组")
                cursor = conn.execute(
                    f"UPDATE subscriptions SET group_name=?,updated_at=? WHERE id IN ({placeholders})",
                    (group, utcnow(), *params),
                )
            else:
                raise HTTPError(400, "批量操作无效")
            conn.commit()
        self.send_json({"ok": True, "affected": cursor.rowcount})

    def subscription_action(self, sub_id, action):
        with self.app.db.connect() as conn:
            if action == "rotate":
                cursor = conn.execute("UPDATE subscriptions SET token=?,updated_at=? WHERE id=?", (random_token(24), utcnow(), sub_id))
            elif action == "reset-traffic":
                cursor = conn.execute("UPDATE subscriptions SET traffic_used_bytes=0,upload_used_bytes=0,download_used_bytes=0,updated_at=? WHERE id=?", (utcnow(), sub_id))
            else:
                exists = conn.execute("SELECT 1 FROM subscriptions WHERE id=?", (sub_id,)).fetchone()
                if not exists:
                    raise HTTPError(404, "订阅不存在")
                conn.execute("DELETE FROM access_keys WHERE subscription_id=?", (sub_id,))
                cursor = type("Cursor", (), {"rowcount": 1})()
            conn.commit()
        if not cursor.rowcount:
            raise HTTPError(404, "订阅不存在")
        self.send_json({"ok": True})

    def subscription_access(self, sub_id):
        with self.app.db.connect() as conn:
            if not conn.execute("SELECT 1 FROM subscriptions WHERE id=?", (sub_id,)).fetchone():
                raise HTTPError(404, "订阅不存在")
            keys = rows_to_dicts(conn.execute("SELECT * FROM access_keys WHERE subscription_id=? ORDER BY last_seen_at DESC", (sub_id,)).fetchall())
        self.send_json({"items": keys})

    def list_logs(self, query):
        limit = min(500, max(1, int(query.get("limit", [100])[0])))
        with self.app.db.connect() as conn:
            rows = rows_to_dicts(conn.execute(
                """SELECT l.*,s.name subscription_name FROM access_logs l LEFT JOIN subscriptions s ON s.id=l.subscription_id
                   ORDER BY l.id DESC LIMIT ?""", (limit,)
            ).fetchall())
        self.send_json({"items": rows})

    def settings(self):
        db_path = Path(self.app.db.path)
        size = sum(item.stat().st_size for item in db_path.parent.glob(f"{db_path.name}*") if item.is_file())
        disk = shutil.disk_usage(db_path.parent)
        with self.app.db.connect() as conn:
            access_log_rows = conn.execute("SELECT COUNT(*) FROM access_logs").fetchone()[0]
            usage_report_rows = conn.execute("SELECT COUNT(*) FROM usage_reports").fetchone()[0]
        self.send_json({
            "version": __version__, "public_url": self.app.public_url, "database_bytes": size,
            "usage_reporting": bool(self.app.report_key), "supported_protocols": sorted(SUPPORTED_SCHEMES),
            "disk_free_bytes": disk.free, "disk_total_bytes": disk.total,
            "access_log_rows": access_log_rows, "usage_report_rows": usage_report_rows,
            "log_retention_days": self.app.log_retention_days,
            "usage_retention_days": self.app.usage_retention_days,
            "max_access_log_rows": self.app.max_access_log_rows,
            "max_usage_report_rows": self.app.max_usage_report_rows,
            "max_workers": self.app.max_workers,
            "cleanup_interval_seconds": self.app.cleanup_interval,
            "last_cleanup_at": self.app.last_cleanup_at,
        })

    def change_password(self, session):
        data = self.read_json()
        current, new = str(data.get("current_password", "")), str(data.get("new_password", ""))
        with self.app.db.connect() as conn:
            admin = conn.execute("SELECT * FROM admins WHERE id=?", (session["admin_id"],)).fetchone()
            if not admin or not verify_password(current, admin["password_hash"]):
                raise HTTPError(400, "当前密码错误")
            try:
                encoded = hash_password(new)
            except ValueError as exc:
                raise HTTPError(400, str(exc))
            conn.execute("UPDATE admins SET password_hash=? WHERE id=?", (encoded, admin["id"]))
            conn.execute("DELETE FROM sessions WHERE admin_id=? AND token_hash<>?", (admin["id"], session["token_hash"]))
            conn.commit()
        self.send_json({"ok": True})

    def change_account(self, session):
        data = self.read_json()
        current = str(data.get("current_password", ""))
        username = str(data.get("username", "")).strip()
        password = str(data.get("new_password", ""))
        if password != str(data.get("confirm_password", "")):
            raise HTTPError(400, "两次输入的新密码不一致")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{3,64}", username):
            raise HTTPError(400, "账号需为 3–64 位英文字母、数字、点、横线或下划线")
        with self.app.db.connect() as conn:
            admin = conn.execute("SELECT * FROM admins WHERE id=?", (session["admin_id"],)).fetchone()
            if not admin or not verify_password(current, admin["password_hash"]):
                raise HTTPError(400, "当前密码错误")
            if username == admin["username"] and not password:
                raise HTTPError(400, "账号和密码均未更改")
            try:
                encoded = hash_password(password) if password else admin["password_hash"]
                conn.execute("UPDATE admins SET username=?,password_hash=? WHERE id=?", (username, encoded, admin["id"]))
            except ValueError as exc:
                raise HTTPError(400, str(exc))
            except sqlite3.IntegrityError:
                raise HTTPError(409, "账号已被使用")
            conn.execute("DELETE FROM sessions WHERE admin_id=?", (admin["id"],))
            conn.commit()
        self.send_json({"ok": True}, headers={"Set-Cookie": "subvault_session=; Path=/vault; HttpOnly; SameSite=Strict; Max-Age=0"})

    def _subscription_precheck(self, row):
        if not row["enabled"]:
            return "订阅已停用"
        if row["expires_at"] and row["expires_at"] <= utcnow():
            return "订阅已到期"
        if row["traffic_limit_bytes"] and row["traffic_used_bytes"] >= row["traffic_limit_bytes"]:
            return "流量已用完"
        return ""

    def _record_log(self, conn, sub_id, ip, device, ua, target, allowed, reason, response_bytes=0):
        conn.execute(
            """INSERT INTO access_logs(subscription_id,happened_at,ip,device,user_agent,target,allowed,reason,response_bytes)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (sub_id, utcnow(), ip, device[:160], ua[:500], target[:40], int(allowed), reason[:160], response_bytes),
        )

    def _check_access(self, token, target):
        ip = self.client_ip()
        ua = self.headers.get("User-Agent", "未知客户端")
        explicit_device = self.headers.get("X-Device-ID") or self.headers.get("X-Client-ID")
        device_label = (explicit_device or ua or "未知客户端")[:160]
        now_dt = datetime.now(timezone.utc)
        with self.app.db.transaction() as conn:
            row = conn.execute("SELECT * FROM subscriptions WHERE token=?", (token,)).fetchone()
            if not row:
                return None, "订阅链接无效"
            reason = self._subscription_precheck(row)
            if reason:
                self._record_log(conn, row["id"], ip, device_label, ua, target, False, reason)
                return dict(row), reason
            cutoff = (now_dt - timedelta(hours=row["access_window_hours"])).isoformat(timespec="seconds")
            keys = [
                ("ip", stable_hash(ip), ip, row["ip_limit"]),
                ("device", stable_hash(explicit_device or ua), device_label, row["device_limit"]),
            ]
            for kind, value_hash, label, limit in keys:
                existing = conn.execute(
                    "SELECT last_seen_at FROM access_keys WHERE subscription_id=? AND kind=? AND value_hash=?",
                    (row["id"], kind, value_hash),
                ).fetchone()
                count = conn.execute(
                    "SELECT COUNT(*) FROM access_keys WHERE subscription_id=? AND kind=? AND last_seen_at>=?",
                    (row["id"], kind, cutoff),
                ).fetchone()[0]
                is_active = bool(existing and existing["last_seen_at"] >= cutoff)
                if not is_active and limit and count >= limit:
                    reason = "活跃 IP 数量超限" if kind == "ip" else "活跃设备数量超限"
                    self._record_log(conn, row["id"], ip, device_label, ua, target, False, reason)
                    return dict(row), reason
            now = utcnow()
            for kind, value_hash, label, _ in keys:
                conn.execute(
                    """INSERT INTO access_keys(subscription_id,kind,value_hash,label,first_seen_at,last_seen_at,hits)
                       VALUES(?,?,?,?,?,?,1) ON CONFLICT(subscription_id,kind,value_hash)
                       DO UPDATE SET label=excluded.label,last_seen_at=excluded.last_seen_at,hits=access_keys.hits+1""",
                    (row["id"], kind, value_hash, label, now, now),
                )
            conn.execute("UPDATE subscriptions SET last_access_at=? WHERE id=?", (now, row["id"]))
            return dict(row), ""

    def serve_subscription(self, path, query):
        match = re.fullmatch(r"/s/([^/]+)(?:/([^/]+))?", path)
        if not match:
            raise HTTPError(404, "订阅地址无效")
        token, requested_target = match.group(1), (match.group(2) or "auto").lower()
        ua = self.headers.get("User-Agent", "").lower()
        if requested_target == "auto":
            if any(name in ua for name in ("clash", "mihomo", "stash", "clash-verge", "clashx")):
                target = "clash"
            elif any(name in ua for name in ("sing-box", "singbox", "nekobox")):
                target = "singbox"
            elif "surge" in ua:
                target = "surge"
            else:
                target = "base64"
        elif requested_target in ("v2ray", "shadowrocket"):
            target = "base64"
        else:
            target = requested_target
        if target not in TARGET_EXTENSIONS:
            raise HTTPError(404, "订阅格式无效")
        log_target = f"auto→{target}" if requested_target == "auto" else requested_target
        sub, denied = self._check_access(token, log_target)
        if denied:
            raise HTTPError(403 if sub is not None else 410, denied)
        requested_template = query.get("template", [""])[0]
        with self.app.db.connect() as conn:
            template = None
            if requested_template.isdigit():
                template = conn.execute("SELECT * FROM templates WHERE id=? AND target=?", (int(requested_template), target)).fetchone()
            if not template:
                template = conn.execute(
                    """SELECT * FROM templates WHERE target=?
                       ORDER BY CASE WHEN name='Clash Meta 智能分流版' THEN 0 ELSE 1 END,builtin DESC,id LIMIT 1""",
                    (target,),
                ).fetchone()
            nodes = rows_to_dicts(conn.execute(
                """SELECT n.* FROM nodes n JOIN subscription_nodes sn ON sn.node_id=n.id
                   WHERE sn.subscription_id=? AND n.enabled=1 ORDER BY n.id""", (sub["id"],)
            ).fetchall())
        if not template:
            raise HTTPError(404, "没有可用模板")
        try:
            body, content_type = render_subscription(target, template["content"], nodes)
        except ValueError as exc:
            raise HTTPError(400, str(exc))
        expire_epoch = int(datetime.fromisoformat(sub["expires_at"]).timestamp()) if sub["expires_at"] else 0
        headers = {
            "Cache-Control": "no-store",
            "Content-Disposition": f"inline; filename*=UTF-8''{quote(safe_filename(sub['name'], TARGET_EXTENSIONS[target]))}",
            "Subscription-Userinfo": f"upload={sub['upload_used_bytes']}; download={sub['download_used_bytes']}; total={sub['traffic_limit_bytes']}; expire={expire_epoch}",
            "Profile-Title": "base64:" + base64.b64encode(sub["name"].encode("utf-8")).decode("ascii"),
            "Profile-Update-Interval": "24",
            "X-SubVault-Target": target,
        }
        if requested_target == "auto":
            headers["Vary"] = "User-Agent"
        with self.app.db.connect() as conn:
            self._record_log(conn, sub["id"], self.client_ip(), (self.headers.get("X-Device-ID") or self.headers.get("User-Agent", "未知客户端")), self.headers.get("User-Agent", ""), log_target, True, "已返回", len(body))
            conn.commit()
        self.send_bytes(body, content_type, headers=headers)

    def report_usage(self):
        auth = self.headers.get("Authorization", "")
        if not self.app.report_key or auth != f"Bearer {self.app.report_key}":
            raise HTTPError(401, "上报密钥无效")
        data = self.read_json()
        token = str(data.get("subscription_token", ""))
        upload = as_nonnegative_int(data.get("upload_bytes"), "上传流量", 10**15)
        download = as_nonnegative_int(data.get("download_bytes"), "下载流量", 10**15)
        source = str(data.get("source") or self.client_ip())[:160]
        if upload + download <= 0:
            raise HTTPError(400, "本次上报流量必须大于 0")
        with self.app.db.transaction() as conn:
            sub = conn.execute("SELECT * FROM subscriptions WHERE token=?", (token,)).fetchone()
            if not sub:
                raise HTTPError(404, "订阅不存在")
            conn.execute(
                """UPDATE subscriptions SET upload_used_bytes=upload_used_bytes+?,download_used_bytes=download_used_bytes+?,
                   traffic_used_bytes=traffic_used_bytes+?,updated_at=? WHERE id=?""",
                (upload, download, upload + download, utcnow(), sub["id"]),
            )
            conn.execute(
                "INSERT INTO usage_reports(subscription_id,happened_at,source,upload_delta,download_delta) VALUES(?,?,?,?,?)",
                (sub["id"], utcnow(), source, upload, download),
            )
            updated = conn.execute("SELECT * FROM subscriptions WHERE id=?", (sub["id"],)).fetchone()
        remaining = max(0, updated["traffic_limit_bytes"] - updated["traffic_used_bytes"]) if updated["traffic_limit_bytes"] else None
        self.send_json({"ok": True, "traffic_used_bytes": updated["traffic_used_bytes"], "remaining_bytes": remaining, "allowed": not bool(self._subscription_precheck(updated))})


class BoundedHTTPServer(ThreadingHTTPServer):
    """Bound request threads so slow clients cannot exhaust a small VPS."""
    request_queue_size = 64

    def __init__(self, address, handler, max_workers):
        self._slots = threading.BoundedSemaphore(max_workers)
        super().__init__(address, handler)

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(15)
        return request, address

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nRetry-After: 1\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def make_server(host: str, port: int, app: App):
    handler = type("SubVaultHandler", (Handler,), {"app": app})
    return BoundedHTTPServer((host, port), handler, app.max_workers)


def main():
    parser = argparse.ArgumentParser(description="SubVault subscription panel")
    parser.add_argument("--host", default=os.getenv("SUBVAULT_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("SUBVAULT_PORT", "8080")))
    args = parser.parse_args()
    app = App()
    app.cleanup()
    server = make_server(args.host, args.port, app)
    stop = threading.Event()
    def maintenance():
        while not stop.wait(app.cleanup_interval):
            try:
                app.cleanup()
            except Exception as exc:
                print(f"SubVault cleanup failed: {type(exc).__name__}: {exc}")
    threading.Thread(target=maintenance, name="subvault-cleanup", daemon=True).start()
    print(f"SubVault {__version__} listening on {args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()


if __name__ == "__main__":
    main()
