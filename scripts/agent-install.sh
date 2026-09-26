#!/usr/bin/env bash
set -Eeuo pipefail

REPO="${NG_REPO:-a2899882/NexusGate-Sub}"
BRANCH="${NG_BRANCH:-main}"
CONTROLLER=""
TOKEN=""
TOKEN_PROMPT=0

die() { printf '错误：%s\n' "$*" >&2; exit 1; }
info() { printf '\033[1;36m[NexusGate Agent]\033[0m %s\n' "$*"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --server) CONTROLLER="${2:-}"; shift 2 ;;
    --token) TOKEN="${2:-}"; shift 2 ;;
    --token-prompt) TOKEN_PROMPT=1; shift ;;
    --repo) REPO="${2:-}"; shift 2 ;;
    --branch) BRANCH="${2:-}"; shift 2 ;;
    *) die "未知参数：$1" ;;
  esac
done

[[ "${EUID}" -eq 0 ]] || die "请使用 root 运行"
[[ "$CONTROLLER" == https://* ]] || die "--server 必须使用 HTTPS 面板地址"
[[ "$REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die '仓库名称无效'
[[ "$BRANCH" =~ ^[A-Za-z0-9._/-]+$ && "$BRANCH" != *..* && "$BRANCH" != /* ]] || die '分支名称无效'
if [[ "$TOKEN_PROMPT" == 1 ]]; then
  [[ -z "$TOKEN" ]] || die "--token 和 --token-prompt 不能同时使用"
  exec 3<>/dev/tty || die "交互输入令牌需要终端；也可以使用 --token"
fi
[[ "$TOKEN_PROMPT" == 1 || -n "$TOKEN" ]] || die "缺少注册令牌（--token-prompt 或 --token）"
if command -v apt-get >/dev/null; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq && apt-get install -y -qq ca-certificates curl unzip nodejs openssl logrotate
elif command -v dnf >/dev/null; then
  dnf install -y ca-certificates curl unzip nodejs openssl logrotate
elif command -v apk >/dev/null; then
  apk add --no-cache bash ca-certificates curl unzip nodejs openrc openssl logrotate
else
  die "当前安装器支持 Debian/Ubuntu、RHEL 系和 Alpine"
fi
node_major="$(node -p 'process.versions.node.split(`.`)[0]')"
[[ "$node_major" -ge 18 ]] || die "需要 Node.js 18 或更高版本"

case "$(uname -m)" in
  x86_64|amd64) xray_arch="64" ;;
  aarch64|arm64) xray_arch="arm64-v8a" ;;
  armv7l) xray_arch="arm32-v7a" ;;
  *) die "暂不支持的架构：$(uname -m)" ;;
esac

tmp_dir="$(mktemp -d /tmp/nexusgate-agent.XXXXXX)"
trap 'rm -rf -- "$tmp_dir"' EXIT
curl -fL --retry 3 --proto '=https' --proto-redir '=https' \
  "https://raw.githubusercontent.com/${REPO}/${BRANCH}/scripts/agent-install-verify.js" -o "$tmp_dir/verify.js"
node --check "$tmp_dir/verify.js"
CONTROLLER="$(node "$tmp_dir/verify.js" controller "$CONTROLLER")" || die '面板地址无效'
info "下载 Xray-core"
curl -fL --retry 3 --proto '=https' --proto-redir '=https' \
  https://api.github.com/repos/XTLS/Xray-core/releases/latest -o "$tmp_dir/release.json"
mapfile -t xray_release < <(node "$tmp_dir/verify.js" xray "$xray_arch" < "$tmp_dir/release.json")
[[ "${#xray_release[@]}" == 2 && "${xray_release[0]}" == "https://github.com/XTLS/Xray-core/releases/download/"* ]] || die 'Xray 发布元数据校验失败'
curl -fL --retry 3 --proto '=https' --proto-redir '=https' "${xray_release[0]}" -o "$tmp_dir/xray.zip"
printf '%s  %s\n' "${xray_release[1]}" "$tmp_dir/xray.zip" | sha256sum -c - || die 'Xray 发布包 SHA-256 不匹配'
unzip -q "$tmp_dir/xray.zip" xray -d "$tmp_dir/xray"
[[ -s "$tmp_dir/xray/xray" ]] || die 'Xray 发布包中没有可执行文件'
chmod 0755 "$tmp_dir/xray/xray"
for file in agent/agent.js agent/run.sh scripts/agent-update.sh scripts/agent-uninstall.sh \
  scripts/agent-doctor.sh scripts/agent-cert.sh scripts/agent-singbox.sh \
  scripts/agent-logrotate.conf scripts/agent-logrotate-setup.sh; do
  curl -fL --retry 3 --proto '=https' --proto-redir '=https' \
    "https://raw.githubusercontent.com/${REPO}/${BRANCH}/${file}" -o "$tmp_dir/${file##*/}"
done
node --check "$tmp_dir/agent.js"
bash -n "$tmp_dir/"*.sh
if command -v systemctl >/dev/null && [[ -d /run/systemd/system ]]; then
  service_manager=systemd
  for unit in nexusgate-agent nexusgate-xray nexusgate-sing-box; do
    curl -fL --retry 3 --proto '=https' --proto-redir '=https' \
      "https://raw.githubusercontent.com/${REPO}/${BRANCH}/systemd/${unit}.service" -o "$tmp_dir/${unit}.service"
  done
elif command -v rc-service >/dev/null; then
  service_manager=OpenRC
  for unit in nexusgate-agent nexusgate-xray nexusgate-sing-box; do
    curl -fL --retry 3 --proto '=https' --proto-redir '=https' \
      "https://raw.githubusercontent.com/${REPO}/${BRANCH}/openrc/${unit}" -o "$tmp_dir/${unit}"
  done
else
  die '未检测到 systemd 或 OpenRC'
fi
if [[ -s /etc/nexusgate/xray/config.json ]]; then
  "$tmp_dir/xray/xray" run -test -config /etc/nexusgate/xray/config.json \
    || die '新 Xray 与当前运行配置不兼容，保留原核心；请先检查 ng-agent doctor'
fi
if [[ -r /etc/nexusgate/agent.env && -f /opt/nexusgate-agent/agent.js ]]; then
  ( set -a; source /etc/nexusgate/agent.env; set +a
    NG_CONTROLLER="$CONTROLLER" node /opt/nexusgate-agent/agent.js flush-usage ) \
    || die '重装前未能上报旧进程流量计数，保留原核心；请先检查控制面连接'
fi
install -m 0755 "$tmp_dir/xray/xray" /usr/local/bin/xray

install -d -m 0755 /opt/nexusgate-agent
install -m 0644 "$tmp_dir/agent.js" /opt/nexusgate-agent/agent.js
install -m 0755 "$tmp_dir/run.sh" /opt/nexusgate-agent/run.sh
install -m 0755 "$tmp_dir/agent-update.sh" /usr/local/sbin/ng-agent-update
install -m 0755 "$tmp_dir/agent-uninstall.sh" /usr/local/sbin/ng-agent-uninstall
install -m 0755 "$tmp_dir/agent-doctor.sh" /usr/local/sbin/ng-agent-doctor
install -m 0755 "$tmp_dir/agent-cert.sh" /usr/local/sbin/ng-agent-cert
install -m 0755 "$tmp_dir/agent-singbox.sh" /usr/local/sbin/ng-agent-singbox
install -m 0644 "$tmp_dir/agent-logrotate.conf" "$tmp_dir/logrotate.conf"
install -m 0755 "$tmp_dir/agent-logrotate-setup.sh" "$tmp_dir/logrotate-setup.sh"
cat > /usr/local/sbin/ng-agent <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
case "${1:-}" in
  update) exec ng-agent-update ;;
  uninstall) shift; exec ng-agent-uninstall "$@" ;;
  doctor|status) exec ng-agent-doctor ;;
  cert) shift; exec ng-agent-cert "$@" ;;
  engine) shift; exec ng-agent-singbox "$@" ;;
  *) printf 'NexusGate Agent: ng-agent doctor | ng-agent cert | ng-agent engine install | ng-agent update | ng-agent uninstall\n' ;;
esac
EOF
chmod 0644 /opt/nexusgate-agent/agent.js
chmod 0755 /opt/nexusgate-agent/run.sh /usr/local/sbin/ng-agent-update /usr/local/sbin/ng-agent-uninstall /usr/local/sbin/ng-agent-doctor /usr/local/sbin/ng-agent-cert /usr/local/sbin/ng-agent-singbox /usr/local/sbin/ng-agent
install -d -m 0700 /etc/nexusgate /etc/nexusgate/xray /etc/nexusgate/xray/resources /etc/nexusgate/sing-box
install -d -m 0750 /var/log/nexusgate
install -m 0644 "$tmp_dir/logrotate.conf" /etc/nexusgate/agent-logrotate.conf
install -m 0755 "$tmp_dir/logrotate-setup.sh" /usr/local/sbin/ng-agent-logrotate-setup
if [[ ! -f /etc/nexusgate/xray/config.json ]]; then
  printf '{"log":{"loglevel":"warning"},"inbounds":[],"outbounds":[]}\n' > /etc/nexusgate/xray/config.json
fi
chmod 0600 /etc/nexusgate/xray/config.json
if [[ "${NG_INSTALL_ANYTLS:-0}" == 1 ]]; then
  NG_REPO="$REPO" NG_BRANCH="$BRANCH" ng-agent-singbox install || die 'AnyTLS 引擎安装失败；修复后运行 ng-agent engine install'
else
  info '基础节点只安装 Xray；需要 AnyTLS 时在入口机运行 ng-agent engine install。'
fi

info "向控制面注册"
if [[ "$TOKEN_PROMPT" == 1 ]]; then
  printf '粘贴一次性注册令牌（输入不显示）：' >&3
  IFS= read -r -s TOKEN <&3 || die "未能读取注册令牌"
  printf '\n' >&3
  exec 3>&-
fi
[[ -n "$TOKEN" ]] || die '注册令牌不能为空'
enroll_json="$(printf '%s' "$TOKEN" | node -e 'const token=require("node:fs").readFileSync(0,"utf8");process.stdout.write(JSON.stringify({token,hostname:require("node:os").hostname(),version:"0.6.9",system:{platform:process.platform,arch:process.arch}}))')"
TOKEN=''
response="$(printf '%s' "$enroll_json" | curl -fsS --proto '=https' --proto-redir '=https' -H 'content-type: application/json' --data-binary @- "${CONTROLLER%/}/api/agent/enroll")" || die "注册失败，请检查地址和令牌"
enroll_json=''
agent_key="$(RESPONSE_VALUE="$response" node -e 'const r=JSON.parse(process.env.RESPONSE_VALUE); if(!r.agentKey) process.exit(1); process.stdout.write(r.agentKey)')" || die "控制面返回无效"

{
  printf 'NG_CONTROLLER=%q\n' "${CONTROLLER%/}"
  printf 'NG_AGENT_KEY=%q\n' "$agent_key"
  printf 'NG_XRAY_BIN=/usr/local/bin/xray\n'
  printf 'NG_CONFIG_DIR=/etc/nexusgate/xray\n'
  printf 'NG_KEY_FILE=/etc/nexusgate/keys.json\n'
} > /etc/nexusgate/agent.env
chmod 0600 /etc/nexusgate/agent.env

if [[ "$service_manager" == systemd ]]; then
  install -m 0644 "$tmp_dir/nexusgate-agent.service" /etc/systemd/system/nexusgate-agent.service
  install -m 0644 "$tmp_dir/nexusgate-xray.service" /etc/systemd/system/nexusgate-xray.service
  install -m 0644 "$tmp_dir/nexusgate-sing-box.service" /etc/systemd/system/nexusgate-sing-box.service
  systemctl daemon-reload
  systemctl enable nexusgate-xray.service nexusgate-agent.service
  systemctl restart nexusgate-xray.service
  rm -f -- /etc/nexusgate/last-heartbeat.json
  systemctl restart nexusgate-agent.service
else
  install -d -m 0755 /etc/init.d
  install -m 0755 "$tmp_dir/nexusgate-agent" /etc/init.d/nexusgate-agent
  install -m 0755 "$tmp_dir/nexusgate-xray" /etc/init.d/nexusgate-xray
  install -m 0755 "$tmp_dir/nexusgate-sing-box" /etc/init.d/nexusgate-sing-box
  chmod 0755 /etc/init.d/nexusgate-agent /etc/init.d/nexusgate-xray /etc/init.d/nexusgate-sing-box
  rc-update add nexusgate-xray default >/dev/null
  rc-update add nexusgate-agent default >/dev/null
  # Alpine runs the dedicated hourly task through BusyBox crond.
  if [[ -e /etc/init.d/crond ]]; then
    rc-update add crond default >/dev/null 2>&1 || true
    rc-service crond start >/dev/null 2>&1 || true
  fi
  rc-service nexusgate-xray restart
  rm -f -- /etc/nexusgate/last-heartbeat.json
  rc-service nexusgate-agent restart
fi
ng-agent-logrotate-setup
for _ in {1..35}; do
  [[ -s /etc/nexusgate/last-heartbeat.json ]] && break
  sleep 1
done
if [[ ! -s /etc/nexusgate/last-heartbeat.json ]]; then
  if [[ "$service_manager" == systemd ]]; then journalctl -u nexusgate-agent.service -n 35 --no-pager || true; fi
  die 'Agent 注册成功但没有向控制面上报心跳；运行 ng-agent doctor 排查后再重试'
fi
printf '\n\033[1;32mAgent 安装、注册和首次心跳完成（%s）。\033[0m\n' "$service_manager"
printf '后续更新 Agent：ng-agent-update\n'
printf '卸载 Agent：ng-agent uninstall\n'
printf '检查 Agent 与 Xray：ng-agent doctor\n'
if [[ "$service_manager" == systemd ]] && ! systemctl is-active --quiet nexusgate-agent.service; then
  journalctl -u nexusgate-agent.service -n 35 --no-pager || true
  die 'Agent 注册已完成，但服务没有运行；请执行 ng-agent doctor 检查原因'
fi
