import test from 'node:test';
import assert from 'node:assert/strict';
import { classify, handle, receiptValid } from '../api/index.mjs';

const env = { CRON_SECRET: 'fixture-cron-secret', GITHUB_WORKFLOW_DISPATCH_TOKEN: 'fixture-token' };
const now = new Date('2026-10-13T07:05:00Z');
test('Taiwan close opens at 15:05, not before', () => {
  assert.equal(classify('tw_close', now).status, 'DISPATCH');
  assert.equal(classify('tw_close', new Date('2026-10-13T07:04:00Z')).status, 'OUTSIDE_DELIVERY_WINDOW');
});
test('late Taiwan open fails closed without sleeps', () => {
  assert.equal(classify('tw_open', new Date('2026-10-13T01:21:48Z')).status, 'OUTSIDE_DELIVERY_WINDOW');
});
test('DST slots are separate', () => {
  assert.equal(classify('edt', new Date('2026-10-13T13:05:00Z')).status, 'DISPATCH');
  assert.equal(classify('est', new Date('2026-10-13T13:05:00Z')).status, 'INACTIVE_DST_SLOT');
});
test('US-close receipt key is TPE publication date, not prior quote session', () => {
  assert.equal(classify('us_close', new Date('2026-10-13T00:30:00Z')).key, 'us_close:20261013');
});
test('authentication cannot trigger a report', async () => {
  assert.equal((await handle('tw_close', '', { now, env, fetcher: () => { throw Error(); } }))[0], 401);
});
test('artifact or empty message IDs are not delivery', () => {
  assert.equal(receiptValid({ key: 'k', state: 'DELIVERED', telegram_message_ids: [] }, 'k'), false);
});
test('receipt suppresses dispatch', async () => {
  let calls = 0;
  const receipt = { key: 'tw_close:20261013', state: 'DELIVERED', telegram_message_ids: [42] };
  const fetcher = async () => { calls++; return { status: 200, json: async () => ({ content: Buffer.from(JSON.stringify(receipt)).toString('base64') }) }; };
  const result = await handle('tw_close', 'Bearer fixture-cron-secret', { now, env, fetcher });
  assert.equal(result[1].status, 'ALREADY_DELIVERED'); assert.equal(calls, 1);
});
test('404 receipt dispatches exactly once', async () => {
  let calls = 0;
  const result = await handle('tw_close', 'Bearer fixture-cron-secret', { now, env,
    fetcher: async () => ({ status: ++calls === 1 ? 404 : 204 }) });
  assert.equal(result[1].status, 'DISPATCHED'); assert.equal(calls, 2);
});
test('failed receipt check is not a green light', async () => {
  const result = await handle('tw_close', 'Bearer fixture-cron-secret', { now, env, fetcher: async () => ({ status: 403 }) });
  assert.equal(result[1].status, 'RECEIPT_CHECK_UNAVAILABLE');
});
