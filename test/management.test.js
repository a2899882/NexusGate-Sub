const test = require('node:test');
const assert = require('node:assert/strict');
const { spawnSync } = require('node:child_process');
const path = require('node:path');

test('a backup called from another management action leaves no RETURN trap', () => {
  const script = `
    source scripts/nexusgate.sh
    need_root() { :; }
    cp() { :; }
    tar() { :; }
    chmod() { :; }
    info() { :; }
    action() { backup /tmp/nexusgate-test-no-output.tar.gz; :; }
    action
    [[ -z "$(trap -p RETURN)" ]]
  `;
  const result = spawnSync('bash', ['-c', script], {
    cwd: path.join(__dirname, '..'), encoding: 'utf8'
  });
  assert.equal(result.status, 0, result.stderr);
});
