import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { imageBytes, runSearch, failureReason } from '../../src/feature/find_chicken/worker.mjs';
import { parseTracemoe, parseSaucenao } from '../../src/feature/find_chicken/vendor/fish-find-chicken.js';
import { validateFetchUrl } from '../../src/feature/find_chicken/vendor/safe-fetch.js';

const source = 'base64://' + Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p1sAAAAASUVORK5CYII=', 'base64').toString('base64');
const settings = { engines: ['saucenao'], timeout_seconds: 5, max_accounts: 3, strict_consensus: true, proxy: '', browser_executable: '' };

test('only actual image inputs accepted, never local paths or arbitrary protocols', async () => {
  assert.equal(imageBytes('https://example.com/a'), null);
  assert.ok(imageBytes(source).length);
  for (const input of ['file:///etc/passwd', '/etc/passwd', 'C:\\secret.png', 'data:image/png;base64,123', 'base64://bad!', 'base64://']) {
    assert.throws(() => imageBytes(input));
  }
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'find-chicken-'));
  try {
    await assert.rejects(runSearch({ source: 'base64://aGVsbG8=', settings }, dir, () => assert.fail('must not search')));
    await assert.rejects(runSearch({ source, settings: { ...settings, engines: ['not-an-engine'] } }, dir));
  } finally { fs.rmSync(dir, { recursive: true, force: true }); }
});

test('public-only download boundary rejects loopback, mapped IPs, credentials and redirects targets', async () => {
  for (const url of ['http://127.0.0.1/a', 'http://169.254.169.254/a', 'http://[::1]/a', 'http://[::ffff:127.0.0.1]/a', 'http://user:pass@example.com/a', 'file:///etc/passwd']) {
    await assert.rejects(validateFetchUrl(url));
  }
});

test('cached image results avoid engines and settings changes invalidate cache', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'find-chicken-'));
  let requests = 0;
  const search = async (bytes, mime, options) => {
    requests++;
    assert.ok(bytes.length);
    assert.equal(mime, 'image/png');
    assert.deepEqual(options.engines, settings.engines);
    return { candidates: [{ sourceUrl: 'https://x.com/example_account/status/123', authorUrl: 'https://x.com/example_account',
      title: 'Example Author (@example_account) / X', similarity: 0.99 }], enginesUsed: ['saucenao'], failures: [] };
  };
  try {
    const first = await runSearch({ source, settings }, dir, search);
    assert.equal(first.cached, false);
    assert.match(first.text, /example_account/);
    assert.match(first.text, /不代表已核实/);
    const second = await runSearch({ source, settings }, dir, search);
    assert.equal(second.cached, true);
    assert.equal(requests, 1);
    await runSearch({ source, settings: { ...settings, strict_consensus: false } }, dir, search);
    assert.equal(requests, 2);
    const cache = JSON.parse(fs.readFileSync(path.join(dir, 'cache.json')));
    assert.equal(Object.keys(cache).length, 2);
    assert.equal(JSON.stringify(cache).includes('iVBORw0'), false);
    for (const item of Object.values(cache)) item.expires_at = 0;
    fs.writeFileSync(path.join(dir, 'cache.json'), JSON.stringify(cache));
    await runSearch({ source, settings }, dir, search);
    assert.equal(requests, 3);
  } finally { fs.rmSync(dir, { recursive: true, force: true }); }
});

test('engine failures are explicit and sanitized, never cached as hits', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'find-chicken-'));
  try {
    for (const candidateList of [[], [{ sourceUrl: 'https://anilist.co/anime/1', title: '示例动画' }]]) {
      const result = await runSearch({ source, settings }, dir, async () => ({
        candidates: candidateList, enginesUsed: candidateList.length ? ['saucenao'] : [],
        failures: [{ engine: 'lens', reason: 'GOOGLE_CAPTCHA secret:/root/token' }],
      }));
      assert.match(result.text, /需要人机验证/);
      assert.equal(result.text.includes('secret'), false);
      if (candidateList.length) assert.match(result.text, /anilist\.co/);
      else assert.equal(fs.existsSync(path.join(dir, 'cache.json')), false);
    }
    assert.equal(failureReason('request timeout https://proxy/password'), '请求超时');
    assert.equal(failureReason('HTTP 429'), '请求频率或免费额度受限');
  } finally { fs.rmSync(dir, { recursive: true, force: true }); }
});

test('upstream anime and illustration parsers keep real source evidence', () => {
  const anime = parseTracemoe({ result: [{ similarity: 0.98, episode: 2, anilist: { id: 123, title: { native: '示例动画' } } },
    { similarity: 0.2, anilist: { id: 456 } }] });
  assert.equal(anime.length, 1);
  assert.equal(anime[0].sourceUrl, 'https://anilist.co/anime/123');
  assert.match(anime[0].title, /示例动画.*2/);
  const artwork = parseSaucenao('<div class="result"><div class="resultsimilarityinfo">96.5%</div><div class="resulttitle"><strong>示例插画</strong></div><a href="https://www.pixiv.net/artworks/123">作品</a></div>');
  assert.equal(artwork[0].sourceUrl, 'https://www.pixiv.net/artworks/123');
  assert.equal(artwork[0].similarity, 0.965);
});

test('cache write failures never discard a successful engine result', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'find-chicken-'));
  try {
    fs.mkdirSync(path.join(dir, 'cache.json'));
    const result = await runSearch({ source, settings }, dir, async () => ({ candidates: [{ sourceUrl: 'https://anilist.co/anime/123' }], enginesUsed: ['tracemoe'], failures: [] }));
    assert.match(result.text, /anilist\.co/);
  } finally { fs.rmSync(dir, { recursive: true, force: true }); }
});
