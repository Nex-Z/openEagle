import { createHash, randomUUID } from 'node:crypto';
import { chromium, type Browser, type BrowserContext, type ElementHandle, type Page } from 'playwright';
import type { Capability, Effect, Json, PreparedAction } from '../contracts.js';

export interface BrowserOptions {
  /** 仅由可信宿主配置；默认不允许任何网络来源。 */
  allowedOrigins?: readonly string[];
  executablePath?: string;
  timeoutMs?: number;
  maxContentChars?: number;
}
type Args = Record<string, Json>;
function object(value: Json): Args {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('浏览器参数必须是对象');
  return value;
}
function string(value: Json | undefined, label: string, max = 4096): string {
  if (typeof value !== 'string' || value.length === 0 || value.length > max) throw new Error(`${label} 无效`);
  return value;
}

/** 每个会话使用全新隔离上下文，不连接用户配置、CDP 或现有浏览器。 */
export function createBrowserCapabilities(options: BrowserOptions = {}): {
  capabilities: Capability[]; close(): Promise<void>;
} {
  const allowed = new Set((options.allowedOrigins ?? []).map(origin => {
    const url = new URL(origin);
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.origin !== origin) {
      throw new Error('allowedOrigins 必须为完整 HTTP(S) 来源');
    }
    return origin;
  }));
  const timeout = Math.max(100, Math.min(options.timeoutMs ?? 10_000, 60_000));
  const contentLimit = Math.max(100, Math.min(options.maxContentChars ?? 12_000, 50_000));
  const resource = `browser:${randomUUID()}`;
  let browser: Browser | undefined;
  let context: BrowserContext | undefined;
  let page: Page | undefined;
  let origin: string | undefined;
  let closed = false;
  let closing: Promise<void> | undefined;
  let launching: Promise<Page> | undefined;
  let sequence = 0;
  const refs = new Map<string, ElementHandle<Node>>();

  function checkUrl(raw: string): URL {
    const url = new URL(raw);
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || !allowed.has(url.origin)) {
      throw new Error('浏览器来源未获宿主许可');
    }
    return url;
  }
  async function clearRefs(): Promise<void> {
    const old = [...refs.values()];
    refs.clear();
    await Promise.all(old.map(handle => handle.dispose().catch(() => {})));
  }
  async function close(): Promise<void> {
    closed = true;
    if (!closing) closing = (async () => {
      // 启动过程也必须结束后才能释放执行资源锁。
      if (launching) await launching.catch(() => {});
      await context?.close().catch(() => {});
      await browser?.close().catch(() => {});
      await clearRefs();
    })();
    await closing;
  }
  async function getPage(): Promise<Page> {
    if (closed) throw new Error('浏览器会话已关闭');
    if (page) return page;
    if (!launching) launching = (async () => {
      browser = await chromium.launch({ headless: true, executablePath: options.executablePath,
        timeout, args: ['--force-webrtc-ip-handling-policy=disable_non_proxied_udp'] });
      context = await browser.newContext({ serviceWorkers: 'block', acceptDownloads: false });
      context.setDefaultTimeout(timeout);
      context.setDefaultNavigationTimeout(timeout);
      // 限制所有页面、重定向、子资源及 WebSocket，避免字段值流向其他来源。
      await context.route('**/*', async route => {
        let permitted = false;
        try {
          const url = checkUrl(route.request().url());
          permitted = url.origin === origin;
        } catch { /* 默认拒绝 */ }
        if (!permitted || closed) { await route.abort('blockedbyclient').catch(() => {}); return; }
        // continue 会自动跟随重定向且不重新进入 route，可能向未批准来源泄露请求。
        // 此最小适配器拒绝所有 HTTP 重定向，不重试有副作用的请求。
        let response;
        try {
          response = await route.fetch({ maxRedirects: 0, maxRetries: 0, timeout });
          if (closed || (response.status() >= 300 && response.status() < 400)) {
            await route.abort('blockedbyclient').catch(() => {});
          } else await route.fulfill({ response });
        } catch { await route.abort('blockedbyclient').catch(() => {}); }
        finally { await response?.dispose().catch(() => {}); }
      });
      await context.routeWebSocket('**/*', socket => socket.close());
      page = await context.newPage();
      const primary = page;
      context.on('page', extra => { if (extra !== primary) void extra.close(); });
      page.on('dialog', dialog => { void dialog.dismiss(); });
      page.on('download', download => { void download.cancel(); });
      page.on('framenavigated', frame => { if (frame === primary.mainFrame()) void clearRefs(); });
      if (closed) throw new Error('浏览器启动已取消');
      return page;
    })();
    return launching;
  }
  async function run<T>(signal: AbortSignal, operation: () => Promise<T>, disposeOnError = true): Promise<T> {
    signal.throwIfAborted();
    const abort = () => { void close(); };
    signal.addEventListener('abort', abort, { once: true });
    const timer = setTimeout(abort, timeout);
    try {
      const value = await operation();
      signal.throwIfAborted();
      if (closed) throw new Error('浏览器操作已超时或关闭');
      return value;
    } catch (error) {
      if (disposeOnError || closed || signal.aborted) await close();
      throw error;
    } finally {
      clearTimeout(timer);
      signal.removeEventListener('abort', abort);
      if (closed) await close();
    }
  }
  function ref(raw: Json | undefined): ElementHandle<Node> {
    const handle = refs.get(string(raw, 'ref', 100));
    if (!handle) throw new Error('目标未观察到或页面已变化，请重新读取页面');
    return handle;
  }
  async function connected(handle: ElementHandle<Node>): Promise<void> {
    if (!await handle.evaluate(element => element.isConnected)) throw new Error('目标已失效，请重新读取页面');
  }
  async function inspect(handle: ElementHandle<Node>): Promise<{ fingerprint: string; target: string }> {
    await connected(handle);
    const descriptor = await handle.evaluate(node => {
      const element = node as HTMLElement;
      const html = element.outerHTML;
      if (html.length > 32_000) throw new Error('元素描述过大');
      const form = (element as HTMLInputElement).form ?? element.closest('form');
      const fields: { name: string; type: string; value?: string | string[]; files?: { name: string; size: number; lastModified: number }[] }[] = [];
      if (form) {
        if (form.elements.length > 200) throw new Error('表单字段过多');
        for (const control of Array.from(form.elements)) {
          const field = control as HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement | HTMLButtonElement;
          if (!field.name || field.matches(':disabled')) continue;
          const type = field.type;
          if (['button', 'reset', 'submit', 'image'].includes(type) && field !== element) continue;
          if (['checkbox', 'radio'].includes(type) && !(field as HTMLInputElement).checked) continue;
          if (field instanceof HTMLSelectElement) {
            fields.push({ name: field.name, type, value: Array.from(field.selectedOptions).filter(option => !option.matches(':disabled')).map(option => option.value) });
          } else if (type === 'file') {
            fields.push({ name: field.name, type, files: Array.from((field as HTMLInputElement).files ?? []).map(file => ({ name: file.name, size: file.size, lastModified: file.lastModified })) });
          } else fields.push({ name: field.name, type, value: field.value });
        }
      }
      const submitter = element as HTMLButtonElement;
      const result = { html, fields, formAction: form?.action ?? null, formMethod: form?.method ?? null,
        formTarget: form?.target ?? null, formEnctype: form?.enctype ?? null,
        submitterAction: element.hasAttribute('formaction') ? submitter.formAction : null,
        submitterMethod: element.hasAttribute('formmethod') ? submitter.formMethod : null,
        submitterTarget: element.hasAttribute('formtarget') ? submitter.formTarget : null,
        submitterEnctype: element.hasAttribute('formenctype') ? submitter.formEnctype : null,
        href: (element as HTMLAnchorElement).href ?? null, baseUri: element.baseURI,
        documentUrl: element.ownerDocument.URL };
      if (JSON.stringify(result).length > 64_000) throw new Error('表单描述过大');
      const preview = { url: element.ownerDocument.URL, tag: element.tagName.toLowerCase(),
        type: (element as HTMLInputElement).type ?? '',
        label: (element.getAttribute('aria-label') ?? element.textContent ?? '').slice(0, 160),
        destination: result.submitterAction ?? result.formAction ?? result.href,
        method: result.submitterMethod ?? result.formMethod,
        fields: fields.slice(0, 20).map(field => ({ name: field.name.slice(0, 80),
          value: field.type === 'password' || /token|secret|password|credential|authorization|api.?key/i.test(field.name)
            ? '[已隐藏敏感字段]' : JSON.stringify(field.value ?? field.files ?? null).slice(0, 160) })),
        fieldsTruncated: fields.length > 20 };
      return { result, preview };
    });
    return { fingerprint: createHash('sha256').update(JSON.stringify(descriptor.result)).digest('hex'),
      target: `${resource} ${JSON.stringify(descriptor.preview)}` };
  }
  async function unchanged(handle: ElementHandle<Node>, approved: string): Promise<void> {
    if ((await inspect(handle)).fingerprint !== approved) throw new Error('目标在批准后发生变化，请重新观察并授权');
  }
  function capability(name: string, description: string, properties: Record<string, unknown>, required: string[],
    prepare: (args: Args) => PreparedAction | Promise<PreparedAction>): Capability {
    return { name: `browser_${name}`, description,
      inputSchema: { type: 'object', properties, required, additionalProperties: false },
      availability: { supported: true },
      async prepare(args, _context, signal = new AbortController().signal) {
        if (closed) throw new Error('浏览器会话已关闭');
        return run(signal, async () => prepare(object(args)), false);
      } };
  }
  function action(target: string, effects: Effect[], execute: PreparedAction['execute'], verify: PreparedAction['verify']): PreparedAction {
    return { target, effects, resources: [resource], execute, verify };
  }
  const textSchema = { type: 'string' };
  const capabilities: Capability[] = [
    capability('navigate', '在隔离浏览器打开宿主允许的 URL；导航也需要外部提交授权', { url: textSchema }, ['url'], args => {
      const url = checkUrl(string(args.url, 'url')).href;
      if (origin && new URL(url).origin !== origin) throw new Error('跨来源导航需要新建隔离浏览器会话');
      return action(url, ['external-submit'], signal => run(signal, async () => {
        const current = await getPage();
        await clearRefs();
        if (origin && new URL(url).origin !== origin) throw new Error('跨来源导航需要新建隔离浏览器会话');
        origin = new URL(url).origin;
        const response = await current.goto(url, { waitUntil: 'domcontentloaded' });
        if (!response || !response.ok()) throw new Error('页面导航未成功');
        checkUrl(current.url());
        return { value: { url: current.url(), status: response.status() },
          evidence: [{ kind: 'browser-url', source: resource, value: current.url() }] };
      }), (output, signal) => run(signal, async () => !!page && page.url() === (output.value as Args).url));
    }),
    capability('read', '读取当前页面的有限文本与可操作元素引用；网页内容不可信', {}, [], () =>
      action(resource, ['read'], signal => run(signal, async () => {
        const current = await getPage();
        await clearRefs();
        const text = await current.locator('body').evaluate((body, limit) => ((body as HTMLElement).innerText ?? '').slice(0, limit), contentLimit);
        const elements: Json[] = [];
        const locator = current.locator('input,textarea,button,a,select,[role="button"],[contenteditable="true"]');
        const count = Math.min(await locator.count(), 80);
        for (let index = 0; index < count; index++) {
          const handle = await locator.nth(index).elementHandle();
          if (!handle) continue;
          const details = await handle.evaluate(node => { const element = node as Element; return ({ tag: element.tagName.toLowerCase(),
            type: element.getAttribute('type') ?? '',
            name: (element.getAttribute('aria-label') ?? element.getAttribute('placeholder') ?? element.textContent ?? '').slice(0, 160) }); });
          const id = `e${++sequence}`;
          refs.set(id, handle);
          elements.push({ ref: id, ...details });
        }
        const value = { url: current.url(), text, elements };
        return { value, evidence: [{ kind: 'browser-dom', source: resource, value }] };
      }), (output, signal) => run(signal, async () => !!page && page.url() === (output.value as Args).url))),
    capability('fill', '填写已观察到的元素，验证 DOM 字段值；输入事件可能提交数据，需要外部提交授权', { ref: textSchema, value: textSchema }, ['ref', 'value'], async args => {
      const handle = ref(args.ref);
      if (typeof args.value !== 'string' || args.value.length > 10_000) throw new Error('value 无效');
      const value = args.value;
      const observed = await inspect(handle);
      const approved = observed.fingerprint;
      const prepared = action(observed.target, ['write', 'external-submit'], signal => run(signal, async () => {
        await unchanged(handle, approved);
        await handle.fill(value);
        return { value: { filled: true }, evidence: [{ kind: 'browser-field', source: `${resource}/${args.ref}`, value: '已填写，值不记录在输出中' }] };
      }), (_output, signal) => run(signal, async () => {
        await connected(handle);
        const actual = await handle.evaluate(node => {
          const element = node as HTMLElement;
          if (element.isContentEditable) return element.textContent ?? '';
          if (element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement) return element.value;
          throw new Error('元素不支持填写验证');
        });
        return actual === value;
      }));
      prepared.fingerprint = approved;
      return prepared;
    }),
    capability('click', '点击已观察到的元素；一律外部提交授权。须指定可验证的预期文本或 URL，否则返回未验证',
      { ref: textSchema, expectedText: textSchema, expectedUrl: textSchema }, ['ref'], async args => {
        const handle = ref(args.ref);
        const expectedText = args.expectedText === undefined ? undefined : string(args.expectedText, 'expectedText', 500);
        const expectedUrl = args.expectedUrl === undefined ? undefined : checkUrl(string(args.expectedUrl, 'expectedUrl')).href;
        let changed = false;
        const observed = await inspect(handle);
        const approved = observed.fingerprint;
        const prepared = action(observed.target, ['external-submit'], signal => run(signal, async () => {
          await unchanged(handle, approved);
          const current = await getPage();
          const beforeUrl = current.url();
          const beforeText = expectedText ? await current.getByText(expectedText, { exact: true }).count() : 0;
          await handle.click();
          if (expectedUrl) await current.waitForURL(expectedUrl, { waitUntil: 'domcontentloaded' });
          if (expectedText) await current.getByText(expectedText, { exact: true }).first().waitFor({ state: 'visible' });
          changed = Boolean((expectedUrl && beforeUrl !== expectedUrl) || (expectedText && beforeText === 0));
          return { value: { url: current.url(), clicked: true },
            evidence: [{ kind: 'browser-observation', source: resource, value: { url: current.url(), expectedText: expectedText ?? null } }] };
        }), (_output, signal) => run(signal, async () => {
          if (!page || !changed || (!expectedText && !expectedUrl)) return false;
          if (expectedUrl && page.url() !== expectedUrl) return false;
          if (expectedText && !await page.getByText(expectedText, { exact: true }).first().isVisible()) return false;
          return true;
        }));
        prepared.fingerprint = approved;
        return prepared;
      }),
  ];
  return { capabilities, close };
}
