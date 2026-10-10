import { spawn } from 'node:child_process';
import { open, realpath, lstat, rename, link, unlink } from 'node:fs/promises';
import path from 'node:path';
import { createHash, randomUUID } from 'node:crypto';
import type { Capability, ActionOutput } from '../contracts.js';
import type { CliOptions } from './cli.js';
import { resolveWorkspaceMutationPath, captureWorkspaceDirectorySnapshot } from './reused-workspace.js';

export interface WindowsTrustedOptions { mode: 'trusted-workspace' }
const warning = 'Windows 显式可信本地工作区：普通用户权限，非 OS 沙箱，不抵御恶意并发替换；';
const quarantined = new Set<string>();
const MAX = 1024 * 1024;
const digest = (b: Buffer) => createHash('sha256').update(b).digest('hex');
const identity = (s: { dev: number; ino: number }) => `${s.dev}:${s.ino}`;
function enabled(options: WindowsTrustedOptions) {
  if (options?.mode !== 'trusted-workspace' || process.platform !== 'win32') throw new Error('需要 Windows trusted-workspace 显式授权');
}
function relativeName(value: unknown): string {
  if (typeof value !== 'string' || !value || value.length > 2048 || /[\\:\x00-\x1f<>"|?*]/.test(value) || path.isAbsolute(value)) throw new Error('拒绝 Windows 非规范相对路径');
  for (const part of value.split('/')) {
    if (!part || part === '.' || part === '..' || /[. ]$/.test(part) || /^(con|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³])(?:\.|$)/i.test(part)) throw new Error('拒绝 Windows 保留名或路径越界');
  }
  return value;
}
async function directory(root: string) {
  if (!path.isAbsolute(root) || root.startsWith('\\\\')) throw new Error('仅支持本地绝对工作区');
  const resolved = path.resolve(root);
  const canonicalRoot = await realpath(resolved);
  const within=(a:string,b:string)=>{const rel=path.relative(a,b);return rel==='' || (rel!=='..'&&!rel.startsWith(`..${path.sep}`)&&!path.isAbsolute(rel));};
  for (const blocked of quarantined) if(within(blocked,canonicalRoot)||within(canonicalRoot,blocked)) throw new Error('工作区主进程退出未确认，暂时隔离');
  let cursor = path.parse(resolved).root;
  const ids: string[] = [];
  for (const part of resolved.slice(cursor.length).split(path.sep).filter(Boolean)) {
    cursor = path.join(cursor, part);
    const s = await lstat(cursor);
    if (s.isSymbolicLink() || !s.isDirectory()) throw new Error('拒绝目录 junction/symlink');
    ids.push(identity(s));
  }
  return { root: await realpath(resolved), ids: ids.join('/') };
}
async function target(root: string, name: string) {
  const base = await directory(root);
  const filename = resolveWorkspaceMutationPath(base.root, name);
  const parent = await directory(path.dirname(filename));
  const ancestors = captureWorkspaceDirectorySnapshot(base.root, parent.root);
  return { filename, stamp: `${base.ids}|${ancestors.map(s => `${s.device}:${s.inode}`).join('/')}` };
}
async function snapshot(filename: string) {
  let before;
  try { before = await lstat(filename); } catch (e) { if ((e as NodeJS.ErrnoException).code === 'ENOENT') return undefined; throw e; }
  if (!before.isFile() || before.isSymbolicLink() || before.nlink !== 1 || before.size > MAX) throw new Error('仅支持单链接普通小文件');
  const h = await open(filename, 'r');
  try {
    const s = await h.stat();
    if (!s.isFile() || s.nlink !== 1 || identity(s) !== identity(before) || s.size > MAX) throw new Error('文件身份变化');
    const b = Buffer.alloc(MAX + 1); let length = 0;
    while (length < b.length) { const n = await h.read(b, length, b.length - length, null); if (!n.bytesRead) break; length += n.bytesRead; }
    if (length > MAX) throw new Error('文件超限');
    const bytes = b.subarray(0, length);
    return { bytes, hash: digest(bytes), stamp: `${identity(s)}:${s.size}:${s.mtimeMs}:${s.ctimeMs}` };
  } finally { await h.close(); }
}
export function createWindowsTrustedFileCapabilities(options: WindowsTrustedOptions): Capability[] {
  enabled(options);
  return [false, true].map(writing => ({
    name: writing ? 'files_write' : 'files_read', description: warning + '相对路径文本文件，最多 1 MiB；覆盖需破坏性审批。', availability: { supported: true },
    inputSchema: { type: 'object', properties: { path: { type: 'string' }, ...(writing ? { content: { type: 'string' } } : {}) }, required: writing ? ['path', 'content'] : ['path'], additionalProperties: false },
    async prepare(args, context) {
      if (!args || typeof args !== 'object' || Array.isArray(args) || Object.keys(args).some(k => !['path', ...(writing ? ['content'] : [])].includes(k))) throw new Error('文件参数无效');
      const name = relativeName(args.path);
      const content = args.content;
      if (writing && (typeof content !== 'string' || Buffer.byteLength(content) > MAX)) throw new Error('内容超限或无效');
      const root = (await directory(context.workspace)).root;
      const prepared = await target(root, name), before = await snapshot(prepared.filename);
      if (!writing && !before) throw new Error('文件不存在');
      const beforeKey = before ? `${before.stamp}:${before.hash}` : 'missing';
      let used = false, output: ActionOutput | undefined, serialized = '', expected = '';
      return {
        target: prepared.filename, fingerprint: `${prepared.stamp}:${beforeKey}`, resources: [`workspace:${root.toLowerCase()}`], effects: writing ? (before ? ['write', 'destructive'] : ['write']) : ['read'],
        async execute(signal) {
          signal.throwIfAborted(); if (used) throw new Error('批准动作不可重放'); used = true;
          const nowTarget = await target(root, name), now = await snapshot(nowTarget.filename);
          if (nowTarget.stamp !== prepared.stamp || (now ? `${now.stamp}:${now.hash}` : 'missing') !== beforeKey) throw new Error('批准后文件或目录变化');
          if (writing) {
            const bytes = Buffer.from(content as string), temp = `${prepared.filename}.${randomUUID()}.tmp`;
            const h = await open(temp, 'wx');
            try {
              try { await h.writeFile(bytes); await h.sync(); } finally { await h.close(); }
              signal.throwIfAborted();
              const last = await target(root, name), existing = await snapshot(last.filename);
              if (last.stamp !== prepared.stamp || (existing ? `${existing.stamp}:${existing.hash}` : 'missing') !== beforeKey) throw new Error('写入前身份变化');
              if (before) await rename(temp, prepared.filename); else await link(temp, prepared.filename);
            } finally { await unlink(temp).catch(e => { if (e.code !== 'ENOENT') throw e; }); }
            expected = digest(bytes);
            if ((await snapshot(prepared.filename))?.hash !== expected) throw new Error('写入验证失败');
            output = { value: { path: prepared.filename, bytes: bytes.length, mode: 'trusted-workspace' }, evidence: [{ kind: 'file-sha256', source: prepared.filename, value: expected }] };
          } else { expected = now!.hash; output = { value: { path: prepared.filename, content: now!.bytes.toString('utf8'), mode: 'trusted-workspace' }, evidence: [{ kind: 'file-sha256', source: prepared.filename, value: expected }] }; }
          serialized = JSON.stringify(output); return output;
        },
        async verify(value, signal) { signal.throwIfAborted(); const latest = await target(root, name); return !!output && value === output && serialized === JSON.stringify(value) && latest.stamp === prepared.stamp && (await snapshot(latest.filename))?.hash === expected; },
      };
    },
  }));
}

export function createWindowsTrustedCliCapability(options: CliOptions & WindowsTrustedOptions): Capability {
  enabled(options);
  const timeout = options.timeoutMs ?? 30000, max = options.maxOutputBytes ?? MAX;
  if (!Number.isSafeInteger(timeout) || timeout < 1 || timeout > 300000 || !Number.isSafeInteger(max) || max < 1 || max > 16 * MAX) throw new Error('CLI 限额无效');
  const profiles = new Map(Object.entries(options.profiles).map(([name, p]) => {
    if (!path.isAbsolute(p.executable) || !/\.exe$/i.test(p.executable) || typeof p.validateArgs !== 'function') throw new Error('必须指定绝对 exe 与参数验证');
    const env: Record<string, string> = {}; const keys = new Set<string>();
    for (const [key, value] of Object.entries(p.env ?? {})) { if (keys.has(key.toLowerCase()) || /[=\0]/.test(key) || value.includes('\0')) throw new Error('环境变量重复或无效'); keys.add(key.toLowerCase()); env[key] = value; }
    return [name, { ...p, env }];
  }));
  return { name: 'cli_run', description: warning + '固定 exe/argv；停止只尽力清理自有 PID 树，无法确认全部后代退出。', availability: { supported: true },
    inputSchema: { type: 'object', properties: { profile: { type: 'string', enum: [...profiles.keys()] }, argv: { type: 'array', items: { type: 'string' } } }, required: ['profile', 'argv'], additionalProperties: false },
    async prepare(args, context) {
      if (!args || typeof args !== 'object' || Array.isArray(args) || typeof args.profile !== 'string' || !Array.isArray(args.argv) || Object.keys(args).some(k => !['profile', 'argv'].includes(k))) throw new Error('CLI 参数无效');
      const p = profiles.get(args.profile); if (!p) throw new Error('未知 profile');
      if (args.argv.length > 128 || args.argv.some(a => typeof a !== 'string' || a.includes('\0') || a.length > 8192)) throw new Error('argv 无效');
      const argv = [...args.argv] as string[];
      if (!p.validateArgs(Object.freeze([...argv]))) throw new Error('argv 未批准');
      const exe = await realpath(p.executable), stat = await lstat(exe);
      if (!stat.isFile()) throw new Error('exe 无效');
      const exeStamp = `${identity(stat)}:${stat.size}:${stat.mtimeMs}:${stat.ctimeMs}`;
      const cwd = await directory(context.workspace);
      let used = false, produced: ActionOutput | undefined, serialized = '', success = false;
      return { target: `${args.profile}: ${JSON.stringify([exe, ...argv])} @ ${cwd.root}`, fingerprint: `${exeStamp}:${cwd.ids}`, resources: [`workspace:${cwd.root.toLowerCase()}`], effects: ['execute'],
        async execute(signal) {
          signal.throwIfAborted(); if (used) throw new Error('批准动作不可重放'); used = true;
          const s = await lstat(exe), current = await directory(cwd.root);
          if (`${identity(s)}:${s.size}:${s.mtimeMs}:${s.ctimeMs}` !== exeStamp || current.ids !== cwd.ids) throw new Error('批准后执行目标变化');
          signal.throwIfAborted();
          return new Promise<ActionOutput>((resolve, reject) => {
            const child = spawn(exe, argv, { cwd: cwd.root, env: p.env, shell: false, detached: false, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] });
            const stdout: Buffer[] = [], stderr: Buffer[] = []; let bytes = 0, settled = false, stopping = false;
            let failure: Error | undefined; let killTimer: ReturnType<typeof setTimeout> | undefined;
            const finish = (code: number | null) => {
              if (settled) return; settled = true; clearTimeout(timer); if (killTimer) clearTimeout(killTimer); signal.removeEventListener('abort', abort);
              child.stdout.destroy(); child.stderr.destroy();
              if (failure) { reject(new Error(`${failure.message}; cleanup=best-effort-unverified`)); return; }
              success = code === 0;
              produced = { value: { stdout: Buffer.concat(stdout).toString('utf8'), stderr: Buffer.concat(stderr).toString('utf8'), exitCode: code, cleanup: 'descendants-unverified', mode: 'trusted-workspace' }, evidence: [{ kind: 'process-exit', source: exe, value: { exitCode: code, cleanup: 'descendants-unverified', cwd: cwd.root } }] };
              serialized = JSON.stringify(produced); resolve(produced);
            };
            const stop = (error: Error) => {
              failure ??= error; if (stopping || settled) return; stopping = true;
              // 只处理仍存活的自有 PID，不按名称杀进程；不是 Job Object containment。
              if (child.pid && child.exitCode === null) {
                const killer = spawn(path.join(process.env.SystemRoot ?? 'C:\\Windows', 'System32', 'taskkill.exe'), ['/PID', String(child.pid), '/T', '/F'], { windowsHide: true, shell: false, stdio: 'ignore', env: {} });
                const killerTimeout = setTimeout(() => killer.kill(), 1500);
                killer.once('error', () => clearTimeout(killerTimeout)); killer.once('exit', () => clearTimeout(killerTimeout));
              }
              killTimer = setTimeout(() => {
                try { child.kill(); } catch { /* report uncertainty below */ }
                if(child.exitCode === null && child.signalCode === null){
                  quarantined.add(cwd.root); failure=new Error(`${failure?.message}; primary-exit-unconfirmed; workspace-quarantined`);
                }
                finish(child.exitCode);
              }, 2000);
            };
            const abort = () => stop(new Error('CLI 已取消'));
            const timer = setTimeout(() => stop(new Error('CLI 超时')), timeout);
            signal.addEventListener('abort', abort, { once: true });
            const collect = (parts: Buffer[]) => (chunk: Buffer) => { if (failure) return; bytes += chunk.length; if (bytes > max) stop(new Error('CLI 输出超限')); else parts.push(chunk); };
            child.stdout.on('data', collect(stdout)); child.stderr.on('data', collect(stderr));
            child.once('error', e => { if(child.pid && child.exitCode===null && child.signalCode===null) stop(e); else {failure=e;finish(null);} });
            child.once('exit',()=>{quarantined.delete(cwd.root);});
            child.once('close', code => finish(code));
            if (signal.aborted) abort();
          });
        },
        async verify(output, signal) { signal.throwIfAborted(); return success && output === produced && serialized === JSON.stringify(output); },
      };
    },
  };
}
