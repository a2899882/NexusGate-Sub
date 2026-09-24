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
  password="$(openssl rand -base64 24 | tr -d '\n')"
  password_b64="$(printf %s "$password" | base64 -w0)"
  report_key="$(openssl rand -hex 32)"
  cat > /etc/nexusgate-subvault.env <<EOF
SUBVAULT_HOST=127.0.0.1
SUBVAULT_PORT=8790
SUBVAULT_DATA_DIR=/var/lib/nexusgate-subvault
SUBVAULT_PUBLIC_URL=https://${domain}/vault
SUBVAULT_COOKIE_SECURE=1
SUBVAULT_ADMIN_USER=admin
SUBVAULT_ADMIN_PASSWORD_B64=${password_b64}
SUBVAULT_USAGE_REPORT_KEY=${report_key}
EOF
  chmod 0600 /etc/nexusgate-subvault.env
else
  install -d -o nexusgate-subvault -g nexusgate-subvault -m 0700 /var/lib/nexusgate-subvault
fi

install -m 0644 /opt/nexusgate/systemd/nexusgate-subvault.service /etc/systemd/system/nexusgate-subvault.service
systemctl daemon-reload
systemctl enable --now nexusgate-subvault
for _ in {1..30}; do
  if curl -fsS http://127.0.0.1:8790/healthz >/dev/null 2>&1; then break; fi
  sleep 1
done
curl -fsS http://127.0.0.1:8790/healthz >/dev/null || {
  systemctl status nexusgate-subvault --no-pager || true
  echo 'SubVault 启动失败，原面板仍然可用' >&2; exit 1;
}

if ! grep -qF 'handle_path /vault/*' "$config"; then
  backup="$(mktemp "$backup_dir/nexusgate.XXXXXX")"
  cp -a "$config" "$backup"
  python3 - "$config" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1])
body = path.read_text()
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
path.write_text(body)
PY
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
else
  caddy validate --config /etc/caddy/Caddyfile
fi
printf '\n独立订阅地址：https://%s/vault/\n账号：admin\n' "$domain"
if [[ -n "${password:-}" ]]; then
  printf '初始密码：%s\n请保存密码；后续运行不会再次显示。\n' "$password"
else
  printf '原有账号和数据已保留。若安装中断时没有记下 SubVault 密码，可运行 ng sub-reset-password。\n'
fi
