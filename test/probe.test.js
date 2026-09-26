'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const net = require('node:net');

test('hop diagnostic measures only the installed relay outbound and never changes its resource', async (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'nexusgate-probe-'));
  process.env.NG_CONFIG_DIR = dir;
  const { probeHop } = require('../agent/agent');
  const resources = path.join(dir, 'resources');
  fs.mkdirSync(resources);
  const listener = net.createServer((socket) => socket.end());
  await new Promise((resolve) => listener.listen(0, '127.0.0.1', resolve));
  t.after(async () => { listener.close(); fs.rmSync(dir, { recursive:true, force:true }); });
  const port = listener.address().port;
  const file = path.join(resources, 'relay-test.json');
  const resource = { meta:{ kind:'relay' }, outbounds:[{ protocol:'shadowsocks', settings:{ servers:[{
    address:'127.0.0.1', port, password:'leave-unchanged' }] } }] };
  fs.writeFileSync(file, JSON.stringify(resource));
  const before = fs.readFileSync(file, 'utf8');
  const reachable = await probeHop({ resourceId:'relay-test', host:'example.net', port:1 });
  assert.equal(reachable.reachable, true);
  assert.ok(reachable.latencyMs >= 0 && reachable.latencyMs <= 3000);
  assert.equal(fs.readFileSync(file, 'utf8'), before);
  resource.meta.kind = 'direct'; fs.writeFileSync(file, JSON.stringify(resource));
  await assert.rejects(probeHop({ resourceId:'relay-test' }), /转发资源/);
  resource.meta.kind = 'relay'; resource.outbounds[0].settings.servers[0].port = port;
  listener.close();
  await new Promise((resolve) => listener.once('close', resolve));
  fs.writeFileSync(file, JSON.stringify(resource));
  const blocked = await probeHop({ resourceId:'relay-test' });
  assert.equal(blocked.reachable, false);
  assert.equal(blocked.reason, '目标端口拒绝连接');
});
