#!/usr/bin/env bash
set -Eeuo pipefail

# Run only on a fresh test VPS. The two application services share Caddy,
# while their users, credentials and databases remain separate.
[[ "${EUID}" -eq 0 ]] || { echo '请使用 root 运行' >&2; exit 1; }
if [[ -e /opt/nexusgate ]]; then
  echo '已有 NexusGate 安装；本试验安装器只接受新机器' >&2
  exit 1
fi
export NG_REPO=a2899882/NexusGate-Sub
source_dir="$(mktemp -d /tmp/nexusgate-sub-install.XXXXXX)"
trap 'rm -rf -- "$source_dir"' EXIT
curl -fL --retry 3 "https://github.com/${NG_REPO}/archive/refs/heads/main.tar.gz" -o "$source_dir/source.tgz"
mkdir "$source_dir/source"
tar -xzf "$source_dir/source.tgz" -C "$source_dir/source" --strip-components=1
bash "$source_dir/source/scripts/install.sh" --repo "$NG_REPO" "$@"
bash /opt/nexusgate/scripts/subvault-install.sh
