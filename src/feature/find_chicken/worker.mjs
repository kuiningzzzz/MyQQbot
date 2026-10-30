// One bounded search process. The Python host owns admission and its process tree.
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { pathToFileURL } from 'node:url';
import {
  reverseSearch, extractAccounts, dropWeakArtwork, formatReply, closeBrowser,
  ENGINES_VERSION, readJsonFile, writeJsonFileAtomic,
} from './vendor/fish-find-chicken.js';
import { safeFetchBuffer, looksLikeImageBuffer } from './vendor/safe-fetch.js';

const MAX_BYTES = 20 * 1024 * 1024;
export const ENGINES = ['lens', 'saucenao', 'tracemoe', 'iqdb', 'baidu'];

export function imageBytes(source) {
  if (typeof source !== 'string') throw new Error('无效图片');
  if (/^https?:\/\//i.test(source)) return null;
  if (!source.startsWith('base64://')) throw new Error('仅接受QQ图片地址，不读取本地文件');
  const encoded = source.slice(9);
  if (encoded.length > Math.ceil(MAX_BYTES / 3) * 4 || !/^[A-Za-z0-9+/]+={0,2}$/.test(encoded)) throw new Error('图片过大或base64无效');
  return Buffer.from(encoded, 'base64');
}

export function failureReason(reason) {
  const value = String(reason || '');
  if (/CAPTCHA|验证码|人机/i.test(value)) return '需要人机验证';
  if (/429|RATE_LIMIT|限流|配额/i.test(value)) return '请求频率或免费额度受限';
  if (/timeout|超时|aborted/i.test(value)) return '请求超时';
  if (/找不到.*(?:Edge|Chrome)|ENOENT/i.test(value)) return '未找到可用浏览器';
  if (/空结果/.test(value)) return '空结果';
  if (/403|denied|forbidden/i.test(value)) return '服务拒绝访问';
  return '网络或引擎暂不可用';
}

export async function runSearch(input, root, search = reverseSearch) {
  const settings = input.settings || {};
  if (!Array.isArray(settings.engines) || !settings.engines.length || settings.engines.some((e) => !ENGINES.includes(e))) throw new Error('无效引擎配置');
  const bytes = imageBytes(input.source) ?? (await safeFetchBuffer(input.source, MAX_BYTES)).buffer;
  if (!bytes?.length || bytes.length > MAX_BYTES || !looksLikeImageBuffer(bytes)) throw new Error('不是支持的图片（PNG/JPEG/GIF/WebP）或超过20 MiB');
  const mime = bytes[0] === 0xff ? 'image/jpeg' : bytes[0] === 0x89 ? 'image/png' : bytes.toString('ascii', 0, 3) === 'GIF' ? 'image/gif' : 'image/webp';
  const key = crypto.createHash('sha256').update(bytes).update(JSON.stringify({
    version: ENGINES_VERSION, engines: settings.engines, strict: settings.strict_consensus, max: settings.max_accounts,
  })).digest('hex');
  const file = path.join(root, 'cache.json');
  const saved = readJsonFile(file, {});
  const cache = Object.fromEntries(Object.entries(saved && typeof saved === 'object' && !Array.isArray(saved) ? saved : {})
    .filter(([, item]) => item?.expires_at > Date.now() && typeof item?.text === 'string').slice(-500));
  if (cache[key]) return { ...cache[key], cached: true };
  const result = await search(bytes, mime, {
    engines: settings.engines, timeoutMs: settings.timeout_seconds * 1000,
    browserExe: settings.browser_executable, proxy: settings.proxy,
    tempDir: input.temp_dir,
  });
  const { accounts, dropped } = dropWeakArtwork(extractAccounts(result.candidates, {
    max: settings.max_accounts, strictNames: settings.strict_consensus !== false,
  }));
  // Never publish paths, proxy credentials or raw transport exceptions to QQ.
  const failures = result.failures.map((f) => ({ engine: f.engine, reason: failureReason(f.reason) }));
  const text = formatReply({ ...result, accounts, failures, artworkDropped: dropped }).replaceAll('**', '')
    + (accounts.length ? '\n\n以上是相似图片页面中的候选账号，不代表已核实图片人物身份。' : '');
  const response = { text, engines_used: result.enginesUsed, failures, cached: false };
  // Only cache actual matches. Empty/failed searches must be retryable immediately.
  if (result.candidates.length && result.enginesUsed.length) {
    cache[key] = { ...response, expires_at: Date.now() + 30 * 86400_000 };
    const trimmed = Object.fromEntries(Object.entries(cache).slice(-500));
    try { writeJsonFileAtomic(file, trimmed); }
    catch { process.stderr.write('无法写入搜图缓存，本次结果仍可交付。\n'); }
  }
  return response;
}

async function main() {
  const root = path.resolve(process.argv[2]);
  fs.mkdirSync(root, { recursive: true });
  process.env.FIND_CHICKEN_PROFILE_DIR = path.join(root, 'browser-profile');
  // Import uses this variable at module initialization, so the host also sets it.
  let buffer = '';
  for await (const chunk of process.stdin) {
    buffer += chunk;
    if (buffer.length > 30 * 1024 * 1024) throw new Error('输入过大');
  }
  const input = JSON.parse(buffer);
  process.env.FIND_CHICKEN_TEMP_DIR = input.temp_dir;
  try {
    const result = await runSearch(input, root);
    process.stdout.write(JSON.stringify({ ok: true, ...result }));
  } finally {
    await closeBrowser();
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  process.on('SIGTERM', () => { closeBrowser().finally(() => process.exit(1)); });
  main().then(() => process.exit(0), (error) => {
    process.stderr.write(String(error?.message || error).slice(0, 1000));
    process.stdout.write(JSON.stringify({ ok: false, error: '图片下载或搜图处理失败，请检查图片是否过期、格式及网络设置。' }));
    process.exit(1);
  });
}
