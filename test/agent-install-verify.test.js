'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { controllerOrigin, xrayReleaseAsset } = require('../scripts/agent-install-verify');

test('agent management URL requires a clean HTTPS origin', () => {
  assert.equal(controllerOrigin('https://gate.example.com/'), 'https://gate.example.com');
  assert.equal(controllerOrigin('https://gate.example.com:8443'), 'https://gate.example.com:8443');
  for (const value of [
    'http://gate.example.com', 'https://user:pass@gate.example.com',
    'https://gate.example.com/api', 'https://gate.example.com/?token=secret',
    'https://gate.example.com/#fragment', 'not-a-url'
  ]) assert.throws(() => controllerOrigin(value));
});

test('Xray archive must match the published release URL and SHA-256 digest', () => {
  const name = 'Xray-linux-64.zip';
  const url = `https://github.com/XTLS/Xray-core/releases/download/v26.3.27/${name}`;
  const asset = { name, browser_download_url: url, digest: `sha256:${'a'.repeat(64)}` };
  const release = { tag_name: 'v26.3.27', assets: [asset], draft: false, prerelease: false };
  assert.deepEqual(xrayReleaseAsset(release, '64'), { url, sha256: 'a'.repeat(64) });
  assert.throws(() => xrayReleaseAsset(release, 'unknown'));
  assert.throws(() => xrayReleaseAsset({ ...release, assets: [{ ...asset, digest: null }] }, '64'));
  assert.throws(() => xrayReleaseAsset({ ...release, assets: [{ ...asset, browser_download_url: 'https://other.example/xray.zip' }] }, '64'));
  assert.throws(() => xrayReleaseAsset({ ...release, prerelease: true }, '64'));
});
