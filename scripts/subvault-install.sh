#!/usr/bin/env bash
set -Eeuo pipefail

[[ "${EUID}" -eq 0 ]] || { echo '需要 root 权限' >&2; exit 1; }
[[ -f /opt/nexusgate/subvault/app.py && -f /etc/caddy/Caddyfile.d/nexusgate.caddy ]] || {
  echo '请先安装本仓库的 NexusGate 控制面' >&2; exit 1;
}
config=/etc/caddy/Caddyfile.d/nexusgate.caddy
domain="$(awk 'NF && $1 !~ /^#/ {print $1;exit}' "$config")"
[[ "$domain" =~ ^[A-Za-z0-9.-]+$ ]] || { echo '无法从 Caddy 配置读取域名' >&2; exit 1; }

# The former installer left this backup inside Caddy's wildcard import.
# Caddy then loaded the same site twice and rejected the configuration.
backup_dir=/etc/caddy/nexusgate-backups
install -d -m 0700 "$backup_dir"
legacy_backup="${config}.before-subvault"
if [[ -f "$legacy_backup" ]]; then
  saved_legacy="$(mktemp "$backup_dir/legacy.XXXXXX")"
  mv -- "$legacy_backup" "$saved_legacy"
  echo "已将旧配置备份移出 Caddy 导入目录：$saved_legacy"
fi

if ! command -v python3 >/dev/null || ! command -v openssl >/dev/null; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get install -y -qq python3 openssl
fi
if [[ ! -f /etc/nexusgate-subvault.env ]]; then
  python3 - <<'PY'
import socket
sock = socket.socket()
try:
    sock.bind(("127.0.0.1", 8790))
except OSError as exc:
    raise SystemExit(f"8790 已被占用：{exc}")
finally:
    sock.close()
PY
  getent group nexusgate-subvault >/dev/null || groupadd --system nexusgate-subvault
  id nexusgate-subvault >/dev/null 2>&1 || useradd --system --gid nexusgate-subvault --home-dir /var/lib/nexusgate-subvault --shell /usr/sbin/nologin nexusgate-subvault
  install -d -o nexusgate-subvault -g nexusgate-subvault -m 0700 /var/lib/nexusgate-subvault
  report_key="$(openssl rand -hex 32)"
  cat > /etc/nexusgate-subvault.env <<EOF
SUBVAULT_HOST=127.0.0.1
SUBVAULT_PORT=8790
SUBVAULT_DATA_DIR=/var/lib/nexusgate-subvault
SUBVAULT_PUBLIC_URL=https://${domain}/vault
SUBVAULT_COOKIE_SECURE=1
SUBVAULT_ADMIN_USER=admin
SUBVAULT_USAGE_REPORT_KEY=${report_key}
EOF
  chmod 0600 /etc/nexusgate-subvault.env
else
  install -d -o nexusgate-subvault -g nexusgate-subvault -m 0700 /var/lib/nexusgate-subvault
fi

# Both services keep their own data stores, but administration uses the live
# NexusGate session. The private bridge credential never reaches the browser.
ng_key="$(sed -n 's/^NG_SUBVAULT_BRIDGE_KEY=//p' /etc/nexusgate.env | head -n 1)"
sub_key="$(sed -n 's/^SUBVAULT_BRIDGE_KEY=//p' /etc/nexusgate-subvault.env | head -n 1)"
if [[ -z "$ng_key" || "$ng_key" != "$sub_key" ]]; then
  bridge_key="$(openssl rand -hex 32)"
  sed -i '/^NG_SUBVAULT_BRIDGE_KEY=/d' /etc/nexusgate.env
  printf 'NG_SUBVAULT_BRIDGE_KEY=%s\n' "$bridge_key" >> /etc/nexusgate.env
else
  bridge_key="$ng_key"
fi
sed -i '/^SUBVAULT_BRIDGE_KEY=/d; /^SUBVAULT_AUTH_MODE=/d; /^SUBVAULT_NG_PORT=/d; /^SUBVAULT_ADMIN_PASSWORD_B64=/d; /^SUBVAULT_ADMIN_PASSWORD=/d' /etc/nexusgate-subvault.env
printf 'SUBVAULT_BRIDGE_KEY=%s\nSUBVAULT_AUTH_MODE=nexusgate\nSUBVAULT_NG_PORT=8787\n' "$bridge_key" >> /etc/nexusgate-subvault.env
chmod 0600 /etc/nexusgate.env /etc/nexusgate-subvault.env
systemctl restart nexusgate
for _ in {1..30}; do
  if curl -fsS http://127.0.0.1:8787/healthz >/dev/null 2>&1; then break; fi
  sleep 1
done
curl -fsS http://127.0.0.1:8787/healthz >/dev/null || {
  echo 'NexusGate 启动失败，联合登录未启用' >&2; exit 1;
}

install -m 0644 /opt/nexusgate/systemd/nexusgate-subvault.service /etc/systemd/system/nexusgate-subvault.service
systemctl daemon-reload
systemctl enable nexusgate-subvault
systemctl restart nexusgate-subvault
for _ in {1..30}; do
  if curl -fsS http://127.0.0.1:8790/healthz >/dev/null 2>&1; then break; fi
  sleep 1
done
curl -fsS http://127.0.0.1:8790/healthz >/dev/null || {
  systemctl status nexusgate-subvault --no-pager || true
  echo 'SubVault 启动失败，原面板仍然可用' >&2; exit 1;
}

backup="$(mktemp "$backup_dir/nexusgate.XXXXXX")"
cp -a "$config" "$backup"
changed="$(python3 - "$config" <<'PY'
from pathlib import Path
import re
import sys
path = Path(sys.argv[1])
body = path.read_text()
original = body
if "handle_path /vault/*" not in body:
    needle = "reverse_proxy 127.0.0.1:8787"
    assert body.count(needle) == 1, "无法安全改写 Caddy 配置"
    body = body.replace(needle, """handle /vault {
        redir /vault/ 308
    }
    handle_path /vault/* {
        reverse_proxy 127.0.0.1:8790 {
            header_up X-Real-IP {remote_host}
        }
    }
    handle {
        reverse_proxy 127.0.0.1:8787
    }""", 1)
lines = body.splitlines(keepends=True)
kept = []
index = 0
while index < len(lines):
    if re.fullmatch(r"\s*log\s*\{\s*", lines[index]):
        end, depth = index, 0
        while end < len(lines):
            depth += lines[end].count("{") - lines[end].count("}")
            end += 1
            if depth == 0:
                break
        block = "".join(lines[index:end])
        if depth == 0 and "output file /var/log/caddy/nexusgate-access.log" in block:
            index = end
            continue
    kept.append(lines[index])
    index += 1
body = "".join(kept)
if body != original:
    path.write_text(body)
print("changed" if body != original else "unchanged")
PY
)"
if [[ "$changed" == changed ]]; then
  caddy fmt --overwrite "$config"
  if ! caddy validate --config /etc/caddy/Caddyfile; then
    cp -a "$backup" "$config"
    echo 'Caddy 验证失败，已恢复原配置' >&2; exit 1
  fi
  if ! systemctl reload caddy; then
    cp -a "$backup" "$config"
    systemctl reload caddy || true
    echo 'Caddy 加载失败，已恢复原配置' >&2; exit 1
  fi
  # The old access log contains complete /vault/s/<token> URLs. The managed
  # site no longer writes it, so discard its rotated copies after reload.
  find /var/log/caddy -maxdepth 1 -type f -name 'nexusgate-access.log*' -delete 2>/dev/null || true
else
  rm -f -- "$backup"
  caddy validate --config /etc/caddy/Caddyfile
fi
printf '\n订阅管理已接入 NexusGate： https://%s/\n只需使用 NexusGate 管理员登录。原有 SubVault 数据已保留。\n' "$domain"
