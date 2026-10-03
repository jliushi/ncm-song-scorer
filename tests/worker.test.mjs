import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
// A data URL keeps the Python repository free of a Node package/runtime dependency.
const source = fs.readFileSync(new URL('../workers/play.js', import.meta.url), 'utf8');
const { default: worker } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
const request = (query = '?id=123', method = 'GET') => new Request(`https://example.test/${query}`, { method });

test('rejects unsafe IDs and unsupported methods without upstream requests', async () => {
  for (const id of ['-1', '0', '123abc', '99999999999999999999', '9007199254740993']) {
    const response = await worker.fetch(request(`?id=${id}`));
    assert.equal(response.status, 400);
  }
  assert.equal((await worker.fetch(request('?id=123', 'POST'))).status, 405);
  const preflight = await worker.fetch(request('', 'OPTIONS'));
  assert.equal(preflight.headers.get('Access-Control-Allow-Origin'), '*');
});

test('upstream outcomes are bounded JSON responses with CORS', async t => {
  const original = globalThis.fetch;
  t.after(() => { globalThis.fetch = original; });
  const cases = [
    { upstream: () => new Response(JSON.stringify({ data: [{ url: 'http://m801.music.126.net/a.mp3', fee: 8, br: 128000 }] })), status: 200, url: 'https://m801.music.126.net/a.mp3' },
    { upstream: () => new Response(JSON.stringify({ data: [{ url: null, fee: 1 }] })), status: 200, url: null },
    { upstream: () => new Response(JSON.stringify({ data: [{ url: 'javascript:alert(1)' }] })), status: 200, url: null },
    { upstream: () => new Response('maintenance', { status: 503 }), status: 502, url: null },
    { upstream: () => new Response('<html>not JSON</html>'), status: 502, url: null },
    { upstream: () => { throw new TypeError('network error'); }, status: 502, url: null },
    { upstream: () => { throw new DOMException('timed out', 'TimeoutError'); }, status: 504, url: null },
  ];
  for (const scenario of cases) {
    globalThis.fetch = async (url, options) => {
      assert.equal(new URL(url).host, 'music.163.com');
      assert.ok(options.signal instanceof AbortSignal);
      return scenario.upstream();
    };
    const response = await worker.fetch(request());
    assert.equal(response.status, scenario.status);
    assert.equal(response.headers.get('Access-Control-Allow-Origin'), '*');
    const data = await response.json();
    assert.equal(data.url, scenario.url);
    assert.equal(data.id, 123);
  }
});
