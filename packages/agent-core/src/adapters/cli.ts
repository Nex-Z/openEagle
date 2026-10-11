import { spawn } from 'node:child_process';
import { realpath, stat } from 'node:fs/promises';
import path from 'node:path';
import type { ActionOutput, Capability } from '../contracts.js';

export interface CommandProfile {
  executable: string;
  /** 可信宿主必须明确允许参数；不要注册任意脚本/解释器执行配置。 */
  validateArgs(argv: readonly string[]): boolean;
  env?: Readonly<Record<string, string>>;
}
export interface CliOptions {
  profiles: Readonly<Record<string, CommandProfile>>;
  timeoutMs?: number;
  maxOutputBytes?: number;
}

export function createCliCapability(options: CliOptions): Capability {
  const timeoutMs = options.timeoutMs ?? 30_000;
  const maxOutputBytes = options.maxOutputBytes ?? 1024 * 1024;
  if (!Number.isSafeInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 300_000 || !Number.isSafeInteger(maxOutputBytes) || maxOutputBytes < 1 || maxOutputBytes > 16 * 1024 * 1024) throw new Error('无效 CLI 限制');
  const profiles = new Map(Object.entries(options.profiles).map(([name, profile]) => {
    if (!path.isAbsolute(profile.executable) || typeof profile.validateArgs !== 'function') throw new Error('命令配置必须有绝对可执行路径和参数验证器');
    return [name, { ...profile, env: { ...profile.env } }];
  }));
  return {
    name: 'cli_run', description: '运行可信宿主预先配置的命令。工作目录限制不等于进程沙箱；执行必须审批',
    availability: { supported: process.platform === 'linux', ...(process.platform !== 'linux' ? { reason: '尚未提供 Windows Job Object 或其他平台的进程树生命周期保障，CLI 已禁用' } : {}) },
    inputSchema: { type: 'object', properties: { profile: { type: 'string', enum: [...profiles.keys()] }, argv: { type: 'array', items: { type: 'string' }, maxItems: 128 } }, required: ['profile', 'argv'], additionalProperties: false },
    async prepare(args, context) {
      if (process.platform !== 'linux') throw new Error('当前平台未提供受控进程树执行');
      if (!args || typeof args !== 'object' || Array.isArray(args) || typeof args.profile !== 'string' || !Array.isArray(args.argv) || Object.keys(args).some(k => !['profile', 'argv'].includes(k))) throw new Error('需要命令配置和参数数组');
      const profile = profiles.get(args.profile);
      if (!profile) throw new Error('命令配置未安装');
      if (args.argv.length > 128 || args.argv.some(a => typeof a !== 'string' || a.includes('\0') || a.length > 8192)) throw new Error('命令参数无效');
      const argv = args.argv as string[];
      if (!profile.validateArgs(Object.freeze([...argv]))) throw new Error('命令参数不符合宿主配置');
      const executable = await realpath(profile.executable);
      const executableStat = await stat(executable);
      if (!executableStat.isFile()) throw new Error('可执行文件无效');
      const executableIdentity = `${executableStat.dev}:${executableStat.ino}:${executableStat.size}:${executableStat.mtimeMs}:${executableStat.ctimeMs}`;
      const cwd = await realpath(context.workspace);
      if (!(await stat(cwd)).isDirectory()) throw new Error('工作区必须是目录');
      const commandArgs = [...argv];
      let produced: ActionOutput | undefined;
      let serializedOutput: string | undefined;
      let successful = false;
      return {
        target: `${args.profile}: ${JSON.stringify([executable, ...commandArgs])} @ ${cwd}`,
        fingerprint: executableIdentity, resources: [`workspace:${cwd}`], effects: ['execute'],
        async execute(signal) {
          signal.throwIfAborted();
          const current = await stat(executable);
          if (`${current.dev}:${current.ino}:${current.size}:${current.mtimeMs}:${current.ctimeMs}` !== executableIdentity) throw new Error('可执行文件在审批后发生变化');
          signal.throwIfAborted();
          return await new Promise<ActionOutput>((resolve, reject) => {
            // 独立进程组仅用于清理普通后代；恶意 setsid 逃逸需要宿主容器/cgroup 沙箱。
            const child = spawn(executable, commandArgs, { cwd, env: profile.env, shell: false, detached: true, stdio: ['ignore', 'pipe', 'pipe'] });
            let bytes = 0;
            const stdout: Buffer[] = [];
            const stderr: Buffer[] = [];
            let failure: Error | undefined;
            const kill = () => {
              if (child.pid) {
                try { process.kill(-child.pid, 'SIGKILL'); }
                catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ESRCH') child.kill('SIGKILL'); }
              }
            };
            const stop = (error: Error) => {
              failure ??= error; kill();
              // 即使越出进程组的外部程序持有管道，也不能无限等待 close。
              child.stdout.destroy(); child.stderr.destroy();
            };
            const abort = () => stop(signal.reason instanceof Error ? signal.reason : new Error('CLI 已取消'));
            const timer = setTimeout(() => stop(new Error('CLI 超时')), timeoutMs);
            signal.addEventListener('abort', abort, { once: true });
            if (signal.aborted) abort();
            const collect = (chunks: Buffer[]) => (chunk: Buffer) => {
              if (failure) return;
              bytes += chunk.length;
              if (bytes > maxOutputBytes) { stop(new Error('CLI 输出超过上限')); return; }
              chunks.push(chunk);
            };
            child.stdout.on('data', collect(stdout));
            child.stderr.on('data', collect(stderr));
            child.on('error', error => { failure ??= error; });
            child.on('exit', kill);
            child.on('close', (code, exitSignal) => {
              clearTimeout(timer); signal.removeEventListener('abort', abort); kill();
              if (failure) { reject(failure); return; }
              successful = code === 0 && exitSignal === null;
              produced = { value: { stdout: Buffer.concat(stdout).toString('utf8'), stderr: Buffer.concat(stderr).toString('utf8'), exitCode: code, signal: exitSignal }, evidence: [{ kind: 'process-exit', source: executable, value: { exitCode: code, signal: exitSignal, cwd } }] };
              serializedOutput = JSON.stringify(produced);
              resolve(produced);
            });
          });
        },
        async verify(output, signal) { signal.throwIfAborted(); return successful && output === produced && JSON.stringify(output) === serializedOutput; },
      };
    },
  };
}
