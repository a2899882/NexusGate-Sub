'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { Orchestrator, expireJobLeases } = require('../lib/orchestrator');

test('an expired customer cannot queue a deployment before maintenance suspends the account', async () => {
  const data = {
    servers: [{ id:'entry', name:'入口', role:'relay', status:'online', lastSeenAt:new Date().toISOString() }],
    customers: [{ id:'customer', status:'active', expiresAt:new Date(Date.now() - 1000).toISOString(), usedBytes:0, trafficLimitBytes:0 }],
    chains: [{ id:'chain', topology:'direct', name:'测试', relayProtocol:'shadowsocks-2022-aes128',
      relayServerIds:['entry'], customerIds:['customer'], networkMode:'ipv4' }],
    deployments: [], jobs: []
  };
  const orchestrator = new Orchestrator({ transaction: async (fn) => fn(data) });
  await assert.rejects(orchestrator.deployChain('chain'), /到期/);
  assert.equal(data.deployments.length, 0);
  assert.equal(data.jobs.length, 0);
});

test('lost diagnostic jobs expire without marking a healthy deployment failed', () => {
  const now = Date.now();
  const data = {
    jobs:[{ id:'probe', action:'probe_hop', status:'running', attempts:3, deploymentId:'entry',
      leaseUntil:new Date(now - 1000).toISOString() }],
    deployments:[{ id:'entry', chainId:'chain', status:'active' }], chains:[{ id:'chain', status:'active' }]
  };
  assert.equal(expireJobLeases(data, now), true);
  assert.equal(data.jobs[0].status, 'failed');
  assert.equal(data.deployments[0].status, 'active');
  assert.equal(data.chains[0].status, 'active');
  data.jobs.push({ id:'queued', action:'probe_hop', status:'queued', createdAt:new Date(now - 6 * 60000).toISOString() });
  assert.equal(expireJobLeases(data, now), true);
  assert.equal(data.jobs[1].status, 'failed');
});
