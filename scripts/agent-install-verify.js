'use strict';

function controllerOrigin(value) {
  let url;
  try { url = new URL(String(value)); } catch { throw new Error('面板地址必须是完整的 HTTPS 域名'); }
  if (url.protocol !== 'https:' || !url.hostname || url.username || url.password ||
      url.pathname !== '/' || url.search || url.hash) {
    throw new Error('面板地址只能填写 HTTPS 根地址，不能包含账号、路径或参数');
  }
  return url.origin;
}

function xrayReleaseAsset(release, arch) {
  if (!['64', 'arm64-v8a', 'arm32-v7a'].includes(arch)) throw new Error('不支持的 Xray 架构');
  const name = `Xray-linux-${arch}.zip`;
  const asset = release?.assets?.find((item) => item.name === name);
  const expected = `https://github.com/XTLS/Xray-core/releases/download/${release?.tag_name}/${name}`;
  if (!asset || !/^sha256:[a-f0-9]{64}$/.test(asset.digest || '') ||
      asset.browser_download_url !== expected || release.draft || release.prerelease) {
    throw new Error('Xray 发布元数据缺少可信的下载地址或 SHA-256，请稍后重试');
  }
  return { url: expected, sha256: asset.digest.slice(7) };
}

if (require.main === module) {
  try {
    if (process.argv[2] === 'controller') console.log(controllerOrigin(process.argv[3]));
    else if (process.argv[2] === 'xray') {
      const release = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
      const asset = xrayReleaseAsset(release, process.argv[3]);
      console.log(asset.url);
      console.log(asset.sha256);
    } else throw new Error('未知校验类型');
  } catch (error) { console.error(error.message); process.exitCode = 1; }
}

module.exports = { controllerOrigin, xrayReleaseAsset };
