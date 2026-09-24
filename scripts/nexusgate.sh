#!/usr/bin/env bash
set -Eeuo pipefail

REPO="${NG_REPO:-a2899882/NexusGate-Sub}"
BRANCH="${NG_BRANCH:-main}"

die() { printf '错误：%s\n' "$*" >&2; exit 1; }
info() { printf '\033[1;36m[NexusGate]\033[0m %s\n' "$*"; }
need_root() { [[ "${EUID}" -eq 0 ]] || die "请使用 root 运行"; }

backup() {
  need_root
  local output="${1:-/root/nexusgate-backup-$(date +%Y%m%d-%H%M%S).tar.gz}"
  local stage
  stage="$(mktemp -d /tmp/nexusgate-backup.XXXXXX)"
  trap 'rm -rf -- "$stage"' RETURN
  cp -a /var/lib/nexusgate "$stage/data"
  cp -a /etc/nexusgate.env "$stage/nexusgate.env"
  if [[ -f /var/lib/nexusgate-subvault/subvault.db && -f /etc/nexusgate-subvault.env ]]; then
    mkdir -p "$stage/subvault-data"
    cp -a /var/lib/nexusgate-subvault/. "$stage/subvault-data/"
    # The SQLite online backup API captures a consistent snapshot, including WAL.
    python3 - "$stage/subvault-data/subvault.db" <<'PY'
import sqlite3
import sys
from pathlib import Path
target = Path(sys.argv[1])
snapshot = target.with_name("subvault.snapshot.db")
with sqlite3.connect("file:/var/lib/nexusgate-subvault/subvault.db?mode=ro", uri=True) as source:
    with sqlite3.connect(snapshot) as destination:
        source.backup(destination)
snapshot.replace(target)
for suffix in ("-wal", "-shm"):
    target.with_name(target.name + suffix).unlink(missing_ok=True)
PY
    cp -a /etc/nexusgate-subvault.env "$stage/nexusgate-subvault.env"
  fi
  [[ -f /etc/caddy/Caddyfile.d/nexusgate.caddy ]] && cp -a /etc/caddy/Caddyfile.d/nexusgate.caddy "$stage/nexusgate.caddy"
  tar -czf "$output" -C "$stage" .
  chmod 0600 "$output"
  info "备份已生成：$output"
}

restore() {
  need_root
  local archive="${1:-}"
  if [[ -z "$archive" ]]; then archive="$(find /root -maxdepth 1 -type f -name 'nexusgate-backup-*.tar.gz' -printf '%T@ %p\n' | sort -nr | head -n1 | cut -d' ' -f2-)"; fi
  [[ -f "$archive" ]] || die "未找到备份文件"
  if tar -tzf "$archive" | grep -Eq '(^/|(^|/)\.\.(/|$))'; then die "备份包包含不安全路径"; fi
  local stage safety
  stage="$(mktemp -d /tmp/nexusgate-restore.XXXXXX)"
  trap 'rm -rf -- "$stage"' RETURN
  tar -xzf "$archive" -C "$stage"
  [[ -f "$stage/data/nexusgate.json" && -f "$stage/nexusgate.env" ]] || die "备份包不完整"
  safety="/root/nexusgate-before-restore-$(date +%Y%m%d-%H%M%S).tar.gz"
  backup "$safety"
  local restore_subvault=false
  if [[ -f "$stage/subvault-data/subvault.db" && -f "$stage/nexusgate-subvault.env" ]]; then
    restore_subvault=true
    systemctl stop nexusgate-subvault
  fi
  systemctl stop nexusgate
  find /var/lib/nexusgate -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
  cp -a "$stage/data/." /var/lib/nexusgate/
  chown -R nexusgate:nexusgate /var/lib/nexusgate
  chmod 0700 /var/lib/nexusgate
  cp -a "$stage/nexusgate.env" /etc/nexusgate.env && chmod 0600 /etc/nexusgate.env
  if [[ "$restore_subvault" == true ]]; then
    install -d -o nexusgate-subvault -g nexusgate-subvault -m 0700 /var/lib/nexusgate-subvault
    find /var/lib/nexusgate-subvault -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
    cp -a "$stage/subvault-data/." /var/lib/nexusgate-subvault/
    chown -R nexusgate-subvault:nexusgate-subvault /var/lib/nexusgate-subvault
    cp -a "$stage/nexusgate-subvault.env" /etc/nexusgate-subvault.env
    chmod 0600 /etc/nexusgate-subvault.env
  fi
  if [[ -f "$stage/nexusgate.caddy" ]]; then cp -a "$stage/nexusgate.caddy" /etc/caddy/Caddyfile.d/nexusgate.caddy; fi
  systemctl start nexusgate
  if [[ "$restore_subvault" == true ]]; then systemctl start nexusgate-subvault; fi
  systemctl reload caddy || true
  info "恢复完成；恢复前快照：$safety"
}

update_panel() {
  need_root
  local stage current_backup old_backup
  local -a old_backups=()
  current_backup="/root/nexusgate-before-update-$(date +%Y%m%d-%H%M%S).tar.gz"
  backup "$current_backup"
  stage="$(mktemp -d /tmp/nexusgate-update.XXXXXX)"
  trap 'rm -rf -- "$stage"' RETURN
  curl -fL --retry 3 "https://github.com/${REPO}/archive/refs/heads/${BRANCH}.tar.gz" -o "$stage/source.tgz"
  mkdir -p "$stage/source" && tar -xzf "$stage/source.tgz" -C "$stage/source" --strip-components=1
  [[ -f "$stage/source/server.js" ]] || die "更新包不完整"
  systemctl stop nexusgate
  if systemctl is-enabled nexusgate-subvault >/dev/null 2>&1; then systemctl stop nexusgate-subvault; fi
  cp -a "$stage/source/." /opt/nexusgate/
  install -m 0644 /opt/nexusgate/systemd/nexusgate.service /etc/systemd/system/nexusgate.service
  if [[ -f /etc/nexusgate-subvault.env ]]; then
    install -m 0644 /opt/nexusgate/systemd/nexusgate-subvault.service /etc/systemd/system/nexusgate-subvault.service
  fi
  install -m 0755 /opt/nexusgate/scripts/nexusgate.sh /usr/local/sbin/nexusgate
  chown -R nexusgate:nexusgate /var/lib/nexusgate
  chmod 0700 /var/lib/nexusgate
  [[ ! -f /var/lib/nexusgate/nexusgate.json ]] || chmod 0600 /var/lib/nexusgate/nexusgate.json
  systemctl daemon-reload && systemctl start nexusgate
  if [[ -f /etc/nexusgate-subvault.env ]]; then systemctl start nexusgate-subvault; fi
  local healthy=false
  for _ in {1..30}; do
    if curl -fsS http://127.0.0.1:8787/healthz >/dev/null 2>&1; then healthy=true; break; fi
    sleep 1
  done
  if [[ "$healthy" != true ]]; then
    systemctl status nexusgate --no-pager || true
    journalctl -u nexusgate -n 100 --no-pager || true
    die "更新后控制面启动失败；可使用上方备份恢复"
  fi
  if [[ -f /etc/nexusgate-subvault.env ]] && ! curl -fsS http://127.0.0.1:8790/healthz >/dev/null; then
    journalctl -u nexusgate-subvault -n 60 --no-pager || true
    die "独立订阅服务启动失败；请使用备份恢复"
  fi
  if [[ -f /etc/nexusgate-subvault.env ]]; then
    # Reconcile the shared Caddy site too. This repairs installations that
    # previously stopped after the duplicate-site validation error.
    bash /opt/nexusgate/scripts/subvault-install.sh
  fi
  if [[ -f /etc/nexusgate/agent.env ]]; then
    info '检测到本机同时承载节点，更新本机 Agent'
    bash "$stage/source/scripts/agent-update.sh" || info 'Agent 更新失败；控制面仍可运行，请执行 ng-agent doctor 查看原因'
  fi
  # Only automatic pre-update snapshots are pruned; manual and pre-restore
  # backups remain untouched. Keep five successful rollback points on disk.
  mapfile -t old_backups < <(find /root -maxdepth 1 -type f -name 'nexusgate-before-update-*.tar.gz' -printf '%T@ %p\n' | sort -nr | sed -n '6,$p' | cut -d' ' -f2-)
  for old_backup in "${old_backups[@]}"; do rm -f -- "$old_backup"; done
  info "更新完成；更新前备份：$current_backup"
}

change_domain() {
  need_root
  local domain="${1:-}"
  if [[ -z "$domain" && -r /dev/tty ]]; then read -r -p '新域名：' domain </dev/tty; fi
  [[ "$domain" =~ ^[A-Za-z0-9.-]+$ ]] || die "域名格式不正确"
  sed -i -E "1s/^[^ ]+/${domain}/" /etc/caddy/Caddyfile.d/nexusgate.caddy
  if [[ -f /etc/nexusgate-subvault.env ]]; then
    sed -i -E "s|^SUBVAULT_PUBLIC_URL=.*$|SUBVAULT_PUBLIC_URL=https://${domain}/vault|" /etc/nexusgate-subvault.env
    systemctl restart nexusgate-subvault
  fi
  caddy fmt --overwrite /etc/caddy/Caddyfile.d/nexusgate.caddy
  caddy validate --config /etc/caddy/Caddyfile && systemctl reload caddy
  info "域名已更新为 https://${domain}"
}

certificate_status() {
  need_root
  caddy validate --config /etc/caddy/Caddyfile
  systemctl reload caddy
  info "Caddy 已校验并重新加载；证书会在到期前自动续签"
  journalctl -u caddy -n 30 --no-pager
}

subvault_info() {
  need_root
  [[ -f /etc/nexusgate-subvault.env ]] || die '尚未安装独立订阅服务'
  sed -n 's/^SUBVAULT_PUBLIC_URL=/独立订阅地址：/p' /etc/nexusgate-subvault.env
  du -sh /var/lib/nexusgate-subvault
  df -h /var/lib/nexusgate-subvault | head -n 2
  systemctl status nexusgate-subvault --no-pager
}

subvault_compact() {
  need_root
  [[ -f /var/lib/nexusgate-subvault/subvault.db ]] || die '未找到独立订阅数据库'
  backup "/root/nexusgate-before-sub-compact-$(date +%Y%m%d-%H%M%S).tar.gz"
  systemctl stop nexusgate-subvault
  # A stopped writer and a preceding combined backup make VACUUM safe.
  if ! runuser -u nexusgate-subvault -- python3 -c 'import sqlite3; conn=sqlite3.connect("/var/lib/nexusgate-subvault/subvault.db"); conn.execute("VACUUM"); conn.close()'; then
    systemctl start nexusgate-subvault
    die '数据库压缩失败，服务已重新启动'
  fi
  systemctl start nexusgate-subvault
  info '独立订阅数据库已压缩'
}

subvault_reset_password() {
  need_root
  [[ -f /etc/nexusgate-subvault.env && -f /var/lib/nexusgate-subvault/subvault.db ]] || die '尚未安装独立订阅服务'
  backup "/root/nexusgate-before-sub-password-$(date +%Y%m%d-%H%M%S).tar.gz"
  local next encoded username
  next="$(openssl rand -base64 24 | tr -d '\n')"
  encoded="$(printf %s "$next" | base64 -w0)"
  username="$(runuser -u nexusgate-subvault -- env PYTHONPATH=/opt/nexusgate/subvault NG_SUB_NEW_PASSWORD="$next" python3 - <<'PY'
import os
import sqlite3
from panel.security import hash_password
path = "/var/lib/nexusgate-subvault/subvault.db"
with sqlite3.connect(path) as conn:
    admin = conn.execute("SELECT id, username FROM admins ORDER BY id LIMIT 1").fetchone()
    if admin is None:
        raise SystemExit("未找到管理员账户")
    conn.execute("UPDATE admins SET password_hash=? WHERE id=?", (hash_password(os.environ["NG_SUB_NEW_PASSWORD"]), admin[0]))
    conn.execute("DELETE FROM sessions")
    print(admin[1])
PY
)"
  sed -i "s|^SUBVAULT_ADMIN_PASSWORD_B64=.*$|SUBVAULT_ADMIN_PASSWORD_B64=${encoded}|" /etc/nexusgate-subvault.env
  chmod 0600 /etc/nexusgate-subvault.env
  info "独立订阅密码已重置；原有会话已退出"
  printf '账号：%s\n新密码：%s\n请保存，不要粘贴到聊天或截图。\n' "$username" "$next"
}

change_account() {
  need_root
  local current="${1:-}" next='' first='' second=''
  if [[ -r /dev/tty ]]; then
    read -r -p '新管理员账号（留空则保持原账号）：' next </dev/tty
    read -r -s -p '新密码（至少 10 位，留空则保持原密码）：' first </dev/tty; printf '\n'
    if [[ -n "$first" ]]; then read -r -s -p '再次输入新密码：' second </dev/tty; printf '\n'; fi
  else
    die "修改管理员账号需要交互终端"
  fi
  [[ "$first" == "$second" ]] || die "两次输入不一致"
  [[ -n "$next" || -n "$first" ]] || die "账号与密码都没有更改"
  [[ -z "$first" || ${#first} -ge 10 ]] || die "密码至少需要 10 个字符"
  systemctl stop nexusgate
  set +e
  NG_DATA_FILE=/var/lib/nexusgate/nexusgate.json NG_NEW_USERNAME="$next" NG_NEW_PASSWORD="$first" node /opt/nexusgate/scripts/reset-password.js "$current"
  local result=$?
  unset first second
  set -e
  if [[ $result -eq 0 ]]; then
    chown nexusgate:nexusgate /var/lib/nexusgate/nexusgate.json
    chmod 0600 /var/lib/nexusgate/nexusgate.json
    sed -i '/^NG_ADMIN_PASSWORD=/d' /etc/nexusgate.env
  fi
  systemctl start nexusgate
  [[ $result -eq 0 ]] || die "管理员账号更新失败"
  info "管理员账号已更新，现有登录会话将在服务重启后失效"
}

uninstall_panel() {
  need_root
  local confirm archive
  if [[ -r /dev/tty ]]; then read -r -p '输入 UNINSTALL 确认卸载（会先自动备份）：' confirm </dev/tty; fi
  [[ "$confirm" == "UNINSTALL" ]] || die "已取消卸载"
  archive="/root/nexusgate-before-uninstall-$(date +%Y%m%d-%H%M%S).tar.gz"
  backup "$archive"
  systemctl disable --now nexusgate.service >/dev/null 2>&1 || true
  systemctl disable --now nexusgate-subvault.service >/dev/null 2>&1 || true
  rm -f -- /etc/systemd/system/nexusgate.service /etc/systemd/system/nexusgate-subvault.service /etc/nexusgate.env /etc/nexusgate-subvault.env /etc/caddy/Caddyfile.d/nexusgate.caddy /usr/local/sbin/ng /usr/local/sbin/nexusgate
  rm -rf -- /opt/nexusgate /var/lib/nexusgate /var/lib/nexusgate-subvault
  systemctl daemon-reload
  systemctl reload caddy >/dev/null 2>&1 || true
  userdel nexusgate >/dev/null 2>&1 || true
  groupdel nexusgate >/dev/null 2>&1 || true
  userdel nexusgate-subvault >/dev/null 2>&1 || true
  groupdel nexusgate-subvault >/dev/null 2>&1 || true
  printf '\nNexusGate 已卸载。可恢复备份：%s\n' "$archive"
}

menu() {
  printf '\nNexusGate 管理菜单\n'
  printf '1. 一键升级\n2. 更换域名\n3. 检查证书\n4. 生成迁移备份\n5. 恢复迁移备份\n6. 修改 NexusGate 管理员账号 / 密码\n7. 查看状态\n8. 重启服务\n9. 查看日志\n10. 卸载面板\n11. 重置独立订阅密码\n0. 退出\n'
  local choice
  read -r -p '请选择：' choice </dev/tty
  case "$choice" in
    1) update_panel ;;
    2) change_domain ;;
    3) certificate_status ;;
    4) backup ;;
    5) restore ;;
    6) change_account ;;
    7) systemctl status nexusgate --no-pager; if [[ -f /etc/nexusgate-subvault.env ]]; then systemctl status nexusgate-subvault --no-pager; fi ;;
    8) need_root; systemctl restart nexusgate caddy; if [[ -f /etc/nexusgate-subvault.env ]]; then systemctl restart nexusgate-subvault; fi; info '已重启' ;;
    9) journalctl -u nexusgate -u nexusgate-subvault -n 120 --no-pager ;;
    10) uninstall_panel ;;
    11) subvault_reset_password ;;
    0) exit 0 ;;
    *) die '无效选择' ;;
  esac
}

case "${1:-menu}" in
  status) systemctl status nexusgate --no-pager; if [[ -f /etc/nexusgate-subvault.env ]]; then systemctl status nexusgate-subvault --no-pager; fi ;;
  restart) need_root; systemctl restart nexusgate caddy; if [[ -f /etc/nexusgate-subvault.env ]]; then systemctl restart nexusgate-subvault; fi ;;
  logs) journalctl -u nexusgate -u nexusgate-subvault -n "${2:-120}" --no-pager ;;
  sub-info) subvault_info ;;
  sub-logs) journalctl -u nexusgate-subvault -n "${2:-120}" --no-pager ;;
  sub-compact) subvault_compact ;;
  sub-reset-password) subvault_reset_password ;;
  backup) backup "${2:-}" ;;
  restore) restore "${2:-}" ;;
  update) update_panel ;;
  domain) change_domain "${2:-}" ;;
  cert) certificate_status ;;
  account|password) change_account "${2:-}" ;;
  uninstall) uninstall_panel ;;
  menu|"") menu ;;
  *) die "用法：nexusgate {status|restart|logs|sub-info|sub-logs|sub-compact|sub-reset-password|backup|restore|update|domain|cert|account|uninstall|menu}" ;;
esac
