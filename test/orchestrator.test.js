'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { Orchestrator } = require('../lib/orchestrator');

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
