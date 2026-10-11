import { constants } from 'node:fs';
import { open, realpath, lstat, rename, link, unlink } from 'node:fs/promises';
import type { FileHandle } from 'node:fs/promises';
import path from 'node:path';
import { createHash, randomUUID } from 'node:crypto';
import type { ActionOutput, Capability, Json } from '../contracts.js';

const MAX_BYTES = 1024 * 1024;
const hash = (data: Buffer) => createHash('sha256').update(data).digest('hex');
const absent = (error: unknown) => (error as NodeJS.ErrnoException).code === 'ENOENT';

function parse(args: Json, writing: boolean): { path: string; content?: string } {
  if (!args || typeof args !== 'object' || Array.isArray(args) || typeof args.path !== 'string') throw new Error('需要相对文件路径');
  if (Object.keys(args).some(key => !['path', ...(writing ? ['content'] : [])].includes(key))) throw new Error('未知文件参数');
  const name = args.path;
  if (!name || name.includes('\0') || name.includes('\\') || path.isAbsolute(name) || name.split('/').some(p => !p || p === '.' || p === '..') || name.includes(':')) throw new Error('拒绝路径越界或非规范路径');
  if (writing && (typeof args.content !== 'string' || Buffer.byteLength(args.content) > MAX_BYTES)) throw new Error('文件内容必须是最多 1 MiB 的文本');
  return { path: name, ...(writing ? { content: args.content as string } : {}) };
}

/** Linux 使用目录描述符锚定路径；其他平台无法提供同等抗目录替换保障，默认关闭。 */
async function anchor(root: string, name: string): Promise<{ filename: string; close(): Promise<void> }> {
  const handles: FileHandle[] = [];
  try {
    let dir = await open(root, constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW);
    handles.push(dir);
    const pieces = name.split('/');
    for (const part of pieces.slice(0, -1)) {
      dir = await open(`/proc/self/fd/${dir.fd}/${part}`, constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW);
      handles.push(dir);
    }
    return { filename: `/proc/self/fd/${dir.fd}/${pieces.at(-1)!}`, close: async () => { await Promise.all(handles.map(h => h.close())); } };
  } catch (error) { await Promise.all(handles.map(h => h.close())); throw error; }
}

async function snapshot(filename: string): Promise<{ digest: string; bytes: Buffer; identity: string } | undefined> {
  let handle: FileHandle;
  try { handle = await open(filename, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK); }
  catch (error) { if (absent(error)) return undefined; throw error; }
  try {
    const stat = await handle.stat();
    if (!stat.isFile() || stat.size > MAX_BYTES) throw new Error('只支持最多 1 MiB 的普通文件');
    const buffer = Buffer.alloc(MAX_BYTES + 1);
    let length = 0;
    while (length < buffer.length) {
      const result = await handle.read(buffer, length, buffer.length - length, null);
      if (!result.bytesRead) break;
      length += result.bytesRead;
    }
    if (length > MAX_BYTES) throw new Error('文件超过大小上限');
    const bytes = buffer.subarray(0, length);
    return { bytes, digest: hash(bytes), identity: `${stat.dev}:${stat.ino}:${stat.mtimeMs}:${stat.ctimeMs}` };
  } finally { await handle.close(); }
}

export function createFileCapabilities(): Capability[] {
  return [false, true].map(writing => ({
    name: writing ? 'files_write' : 'files_read',
    description: writing ? '在工作区内原子写入 UTF-8 文本（最多 1 MiB，父目录必须存在）' : '读取工作区内 UTF-8 文本（最多 1 MiB）',
    inputSchema: { type: 'object', properties: { path: { type: 'string' }, ...(writing ? { content: { type: 'string' } } : {}) }, required: writing ? ['path', 'content'] : ['path'], additionalProperties: false },
    availability: { supported: process.platform === 'linux', ...(process.platform !== 'linux' ? { reason: '当前文件适配器需要 Linux /proc 目录描述符；其他平台尚未实现等效安全路径锚定' } : {}) },
    async prepare(args, context) {
      if (process.platform !== 'linux') throw new Error('当前平台没有安全文件路径适配器');
      const input = parse(args, writing);
      const root = await realpath(context.workspace);
      const target = path.join(root, input.path);
      const prepared = await anchor(root, input.path);
      let before: Awaited<ReturnType<typeof snapshot>>;
      try { before = await snapshot(prepared.filename); } finally { await prepared.close(); }
      if (!writing && !before) throw new Error('文件不存在');
      let produced: ActionOutput | undefined;
      let serializedOutput: string | undefined;
      let expectedDigest: string | undefined;
      return {
        target, fingerprint: before ? `${before.identity}:${before.digest}` : 'missing', resources: [`workspace:${root}`], effects: writing ? (before ? ['write', 'destructive'] : ['write']) : ['read'],
        async execute(signal) {
          signal.throwIfAborted();
          const current = await anchor(root, input.path);
          try {
            const now = await snapshot(current.filename);
            if (now?.identity !== before?.identity || now?.digest !== before?.digest) throw new Error('文件在审批后发生变化，请重新准备操作');
            if (writing) {
              const bytes = Buffer.from(input.content!, 'utf8');
              const temporary = `${current.filename}.${randomUUID()}.tmp`;
              let created = false;
              try {
                const handle = await open(temporary, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600);
                created = true;
                try { await handle.writeFile(bytes); await handle.sync(); } finally { await handle.close(); }
                signal.throwIfAborted();
                // 覆盖替换不会追随符号链接；新增用硬链接保证目标不存在。
                if (before) {
                  const latest = await snapshot(current.filename);
                  if (latest?.identity !== before.identity || latest.digest !== before.digest) throw new Error('目标文件已变化');
                  await rename(temporary, current.filename); created = false;
                } else { await link(temporary, current.filename); }
              } finally { if (created) await unlink(temporary); }
              expectedDigest = hash(bytes);
              const actual = await snapshot(current.filename);
              if (!actual || actual.digest !== expectedDigest) throw new Error('文件写入校验失败');
              produced = { value: { path: target, bytes: bytes.length }, evidence: [{ kind: 'file-sha256', source: target, value: actual.digest }] };
            } else {
              expectedDigest = now!.digest;
              produced = { value: { path: target, content: now!.bytes.toString('utf8') }, evidence: [{ kind: 'file-sha256', source: target, value: now!.digest }] };
            }
            serializedOutput = JSON.stringify(produced);
            return produced;
          } finally { await current.close(); }
        },
        async verify(output, signal) {
          signal.throwIfAborted();
          if (!produced || output !== produced || JSON.stringify(output) !== serializedOutput) return false;
          const current = await anchor(root, input.path);
          try {
            const entry = await lstat(current.filename);
            return entry.isFile() && (await snapshot(current.filename))?.digest === expectedDigest;
          } finally { await current.close(); }
        },
      };
    },
  }));
}
