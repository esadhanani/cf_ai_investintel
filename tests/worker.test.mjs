import test from 'node:test';
import assert from 'node:assert/strict';
import worker from '../src/investintel.ts';

const request = (body, options = {}) => new Request('https://example.test/query', {
  method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify(body), ...options,
});
const env = {AI: {run: async () => ({response: 'Synthetic summary.'})}};

test('summarises input without echoing it or retaining server history', async () => {
  const response = await worker.fetch(request({topic: 'Synthetic source document'}), env);
  assert.equal(response.status, 200);
  assert.equal(response.headers.get('cache-control'), 'no-store');
  const value = await response.json();
  assert.equal(value.summary, 'Synthetic summary.');
  assert.equal('topic' in value, false);
  const history = await worker.fetch(new Request('https://example.test/history'), env);
  assert.equal(history.status, 410);
  assert.equal(JSON.stringify(await history.json()).includes('Synthetic source document'), false);
});

test('forwards only the supplied text and describes source limits', async () => {
  let received;
  const capture = {AI: {run: async (model, input) => {received = {model, input}; return {response: 'ok'};}}};
  await worker.fetch(request({topic: '  Quoted text  '}), capture);
  assert.equal(received.input.messages[1].content, 'Quoted text');
  assert.match(received.input.messages[0].content, /Do not claim access to live news/);
});

test('rejects malformed JSON without calling the model', async () => {
  const response = await worker.fetch(request(null, {body: '{not json'}), {AI:{run:()=>assert.fail('called provider')}});
  assert.equal(response.status, 400);
});

test('rejects missing, empty, array and non-string topics', async () => {
  for (const body of [null, [], {}, {topic: ''}, {topic: '   '}, {topic: 1}]) {
    assert.equal((await worker.fetch(request(body), env)).status, 400);
  }
});

test('rejects overlong input before provider call', async () => {
  assert.equal((await worker.fetch(request({topic:'a'.repeat(8001)}), env)).status, 413);
  assert.equal((await worker.fetch(request({topic:'a'.repeat(33000)}), env)).status, 413);
});

test('limits streamed bodies without relying on content-length', async () => {
  const body = new ReadableStream({start(c) {c.enqueue(new Uint8Array(40000)); c.close();}});
  const req = new Request('https://example.test/query', {method:'POST', headers:{'content-type':'application/json'}, body, duplex:'half'});
  assert.equal((await worker.fetch(req, env)).status, 413);
});

test('returns honest route and method errors', async () => {
  assert.equal((await worker.fetch(new Request('https://example.test/missing'), env)).status, 404);
  const response = await worker.fetch(new Request('https://example.test/query'), env);
  assert.equal(response.status, 405);
  assert.equal(response.headers.get('allow'), 'POST');
  assert.equal((await worker.fetch(request({topic:'example'},{headers:{}}), env)).status, 415);
});

test('handles empty and invalid provider replies', async () => {
  for (const reply of [null, {}, {response:''}, {response:4}]) {
    assert.equal((await worker.fetch(request({topic:'example'}), {AI:{run:async()=>reply}})).status, 502);
  }
});

test('does not disclose provider exception details', async () => {
  const response = await worker.fetch(request({topic:'example'}), {AI:{run:async()=>{throw new Error('private diagnostic');}}});
  assert.equal(response.status, 502);
  assert.equal((await response.text()).includes('private diagnostic'), false);
});
