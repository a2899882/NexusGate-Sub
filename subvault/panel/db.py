import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .security import hash_password


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS admins (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    admin_id INTEGER NOT NULL REFERENCES admins(id) ON DELETE CASCADE,
    csrf_token TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    uri TEXT NOT NULL,
    group_name TEXT NOT NULL DEFAULT '默认',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    target TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    builtin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    group_name TEXT NOT NULL DEFAULT '默认',
    token TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1,
    expires_at TEXT,
    traffic_limit_bytes INTEGER NOT NULL DEFAULT 0,
    traffic_used_bytes INTEGER NOT NULL DEFAULT 0,
    upload_used_bytes INTEGER NOT NULL DEFAULT 0,
    download_used_bytes INTEGER NOT NULL DEFAULT 0,
    ip_limit INTEGER NOT NULL DEFAULT 0,
    device_limit INTEGER NOT NULL DEFAULT 0,
    access_window_hours INTEGER NOT NULL DEFAULT 24,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_access_at TEXT
);

CREATE TABLE IF NOT EXISTS subscription_nodes (
    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    node_id INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    PRIMARY KEY (subscription_id, node_id)
);

CREATE TABLE IF NOT EXISTS access_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN ('ip', 'device')),
    value_hash TEXT NOT NULL,
    label TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    hits INTEGER NOT NULL DEFAULT 1,
    UNIQUE(subscription_id, kind, value_hash)
);

CREATE TABLE IF NOT EXISTS access_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id INTEGER REFERENCES subscriptions(id) ON DELETE SET NULL,
    happened_at TEXT NOT NULL,
    ip TEXT NOT NULL,
    device TEXT NOT NULL,
    user_agent TEXT NOT NULL,
    target TEXT NOT NULL,
    allowed INTEGER NOT NULL,
    reason TEXT NOT NULL,
    response_bytes INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS usage_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id INTEGER REFERENCES subscriptions(id) ON DELETE SET NULL,
    happened_at TEXT NOT NULL,
    source TEXT NOT NULL,
    upload_delta INTEGER NOT NULL,
    download_delta INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_access_keys_active ON access_keys(subscription_id, kind, last_seen_at);
CREATE INDEX IF NOT EXISTS idx_access_logs_time ON access_logs(happened_at DESC);
CREATE INDEX IF NOT EXISTS idx_usage_reports_time ON usage_reports(happened_at DESC);
CREATE INDEX IF NOT EXISTS idx_nodes_group_status ON nodes(group_name,enabled);
CREATE INDEX IF NOT EXISTS idx_subscriptions_status ON subscriptions(enabled,expires_at);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    @contextmanager
    def transaction(self):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self, admin_username: str, admin_password: str) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(subscriptions)")}
            if "group_name" not in columns:
                conn.execute("ALTER TABLE subscriptions ADD COLUMN group_name TEXT NOT NULL DEFAULT '默认'")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_subscriptions_group ON subscriptions(group_name)")
            exists = conn.execute("SELECT 1 FROM admins LIMIT 1").fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO admins(username,password_hash,created_at) VALUES(?,?,?)",
                    (admin_username, hash_password(admin_password), utcnow()),
                )
            self._seed_templates(conn)
            conn.commit()

    @staticmethod
    def _seed_templates(conn: sqlite3.Connection) -> None:
        now = utcnow()
        templates = [
            (
                "通用 Base64",
                "base64",
                "",
            ),
            (
                "Clash Meta 简洁版",
                "clash",
                """mixed-port: 7890
allow-lan: false
mode: rule
log-level: info
ipv6: false
proxies:
{{PROXIES}}
proxy-groups:
  - name: \"节点选择\"
    type: select
    proxies:
{{PROXY_NAMES}}
rules:
  - MATCH,节点选择
""",
            ),
            (
                "Clash Meta 智能分流版",
                "clash",
                """mixed-port: 7890
allow-lan: false
mode: rule
log-level: info
ipv6: false
unified-delay: true
tcp-concurrent: true

proxies:
{{PROXIES}}

proxy-groups:
  - name: \"🚀 节点选择\"
    type: select
    proxies:
      - \"♻️ 自动选择\"
      - \"🛟 故障转移\"
{{PROXY_NAMES}}
      - DIRECT
  - name: \"♻️ 自动选择\"
    type: url-test
    proxies:
{{PROXY_NAMES}}
    url: \"https://www.gstatic.com/generate_204\"
    interval: 300
    tolerance: 80
    lazy: true
  - name: \"🛟 故障转移\"
    type: fallback
    proxies:
{{PROXY_NAMES}}
    url: \"https://www.gstatic.com/generate_204\"
    interval: 300
    lazy: true
  - name: \"🤖 AI 服务\"
    type: select
    proxies: [\"🚀 节点选择\", \"♻️ 自动选择\", DIRECT]
  - name: \"📨 Telegram\"
    type: select
    proxies: [\"🚀 节点选择\", \"♻️ 自动选择\"]
  - name: \"🎬 流媒体\"
    type: select
    proxies: [\"🚀 节点选择\", \"♻️ 自动选择\", DIRECT]
  - name: \"🍎 苹果服务\"
    type: select
    proxies: [DIRECT, \"🚀 节点选择\"]
  - name: \"Ⓜ️ 微软服务\"
    type: select
    proxies: [DIRECT, \"🚀 节点选择\"]
  - name: \"🌍 国外网站\"
    type: select
    proxies: [\"🚀 节点选择\", \"♻️ 自动选择\"]
  - name: \"🐟 漏网之鱼\"
    type: select
    proxies: [\"🚀 节点选择\", \"♻️ 自动选择\", DIRECT]

rule-providers:
  reject:
    type: http
    behavior: domain
    format: yaml
    url: \"https://cdn.jsdelivr.net/gh/Loyalsoldier/clash-rules@release/reject.txt\"
    path: ./ruleset/reject.yaml
    interval: 86400
  private:
    type: http
    behavior: domain
    format: yaml
    url: \"https://cdn.jsdelivr.net/gh/Loyalsoldier/clash-rules@release/private.txt\"
    path: ./ruleset/private.yaml
    interval: 86400
  direct:
    type: http
    behavior: domain
    format: yaml
    url: \"https://cdn.jsdelivr.net/gh/Loyalsoldier/clash-rules@release/direct.txt\"
    path: ./ruleset/direct.yaml
    interval: 86400
  proxy:
    type: http
    behavior: domain
    format: yaml
    url: \"https://cdn.jsdelivr.net/gh/Loyalsoldier/clash-rules@release/proxy.txt\"
    path: ./ruleset/proxy.yaml
    interval: 86400
  apple:
    type: http
    behavior: domain
    format: yaml
    url: \"https://cdn.jsdelivr.net/gh/Loyalsoldier/clash-rules@release/apple.txt\"
    path: ./ruleset/apple.yaml
    interval: 86400
  telegramcidr:
    type: http
    behavior: ipcidr
    format: yaml
    url: \"https://cdn.jsdelivr.net/gh/Loyalsoldier/clash-rules@release/telegramcidr.txt\"
    path: ./ruleset/telegramcidr.yaml
    interval: 86400
  cncidr:
    type: http
    behavior: ipcidr
    format: yaml
    url: \"https://cdn.jsdelivr.net/gh/Loyalsoldier/clash-rules@release/cncidr.txt\"
    path: ./ruleset/cncidr.yaml
    interval: 86400
  lancidr:
    type: http
    behavior: ipcidr
    format: yaml
    url: \"https://cdn.jsdelivr.net/gh/Loyalsoldier/clash-rules@release/lancidr.txt\"
    path: ./ruleset/lancidr.yaml
    interval: 86400

rules:
  - DOMAIN-SUFFIX,openai.com,🤖 AI 服务
  - DOMAIN-SUFFIX,chatgpt.com,🤖 AI 服务
  - DOMAIN-SUFFIX,anthropic.com,🤖 AI 服务
  - DOMAIN-SUFFIX,claude.ai,🤖 AI 服务
  - DOMAIN-SUFFIX,googleapis.com,🌍 国外网站
  - DOMAIN-SUFFIX,youtube.com,🎬 流媒体
  - DOMAIN-SUFFIX,netflix.com,🎬 流媒体
  - DOMAIN-SUFFIX,spotify.com,🎬 流媒体
  - DOMAIN-SUFFIX,microsoft.com,Ⓜ️ 微软服务
  - DOMAIN-SUFFIX,office.com,Ⓜ️ 微软服务
  - RULE-SET,reject,REJECT
  - RULE-SET,private,DIRECT
  - RULE-SET,apple,🍎 苹果服务
  - RULE-SET,direct,DIRECT
  - RULE-SET,lancidr,DIRECT,no-resolve
  - RULE-SET,cncidr,DIRECT,no-resolve
  - GEOIP,LAN,DIRECT,no-resolve
  - GEOIP,CN,DIRECT,no-resolve
  - RULE-SET,telegramcidr,📨 Telegram,no-resolve
  - RULE-SET,proxy,🌍 国外网站
  - MATCH,🐟 漏网之鱼
""",
            ),
            (
                "sing-box 简洁版",
                "singbox",
                """{
  \"log\": {\"level\": \"info\"},
  \"inbounds\": [
    {\"type\": \"mixed\", \"tag\": \"mixed-in\", \"listen\": \"127.0.0.1\", \"listen_port\": 2080}
  ],
  \"outbounds\": [
{{OUTBOUNDS}}
  ],
  \"route\": {\"final\": {{FIRST_TAG}}}
}
""",
            ),
            (
                "Surge 简洁版",
                "surge",
                """[General]
loglevel = notify
skip-proxy = 127.0.0.1, localhost, *.local

[Proxy]
{{SURGE_PROXIES}}

[Proxy Group]
节点选择 = select, {{SURGE_NAMES}}

[Rule]
FINAL,节点选择
""",
            ),
        ]
        for name, target, content in templates:
            conn.execute(
                """INSERT OR IGNORE INTO templates(name,target,content,builtin,created_at,updated_at)
                   VALUES(?,?,?,1,?,?)""",
                (name, target, content, now, now),
            )


def rows_to_dicts(rows):
    return [dict(row) for row in rows]
