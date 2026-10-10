import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, writeFile, readFile, rm, symlink, access } from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { createFileCapabilities } from '../src/adapters/files.js';
import { createCliCapability } from '../src/adapters/cli.js';
import type { TaskContext } from '../src/contracts.js';

const linux = { skip: process.platform !== 'linux' };
const signal = () => new AbortController().signal;
async function fixture() {
  const root = await mkdtemp(path.join(os.tmpdir(), 'eagle-core-'));
  const context: TaskContext = { id: 'test', goal: '验证适配器', workspace: root, facts: [] };
  return { root, context, dispose: () => rm(root, { recursive: true, force: true }) };
}

test('文件准备不写入；实际写入、读取和摘要验证', linux, async () => {
  const f = await fixture();
  try {
    const [read, write] = createFileCapabilities();
    const prepared = await write.prepare({ path: 'note.txt', content: '你好' }, f.context);
    assert.deepEqual(prepared.effects, ['write']);
    await assert.rejects(access(path.join(f.root, 'note.txt')));
    const output = await prepared.execute(signal());
    assert.equal(await prepared.verify(output, signal()), true);
    assert.equal(await prepared.verify({ ...output }, signal()), false);
    const reader = await read.prepare({ path: 'note.txt' }, f.context);
    const content = await reader.execute(signal());
    assert.deepEqual(content.value, { path: path.join(f.root, 'note.txt'), content: '你好' });
    assert.equal(await reader.verify(content, signal()), true);
    const overwrite = await write.prepare({ path: 'note.txt', content: '第二版' }, f.context);
    assert.deepEqual(overwrite.effects, ['write', 'destructive']);
    const second = await overwrite.execute(signal());
    assert.equal(await overwrite.verify(second, signal()), true);
    assert.equal(await prepared.verify(output, signal()), false);
  } finally { await f.dispose(); }
});

test('文件拒绝路径越界、符号链接、超限和审批后变化', linux, async () => {
  const f = await fixture();
  const outside = await fixture();
  try {
    const [read, write] = createFileCapabilities();
    for (const name of ['../escape', 'a/../escape', '/tmp/escape', 'a\\escape', 'a//b', 'a:stream']) await assert.rejects(write.prepare({ path: name, content: 'x' }, f.context));
    await writeFile(path.join(outside.root, 'secret'), 'outside');
    await symlink(outside.root, path.join(f.root, 'escape'));
    await assert.rejects(read.prepare({ path: 'escape/secret' }, f.context));
    await symlink(path.join(outside.root, 'secret'), path.join(f.root, 'link'));
    await assert.rejects(write.prepare({ path: 'link', content: 'changed' }, f.context));
    assert.equal(await readFile(path.join(outside.root, 'secret'), 'utf8'), 'outside');
    await assert.rejects(write.prepare({ path: 'large', content: 'x'.repeat(1024 * 1024 + 1) }, f.context));
    await writeFile(path.join(f.root, 'large'), Buffer.alloc(1024 * 1024 + 1));
    await assert.rejects(read.prepare({ path: 'large' }, f.context));
    const prepared = await write.prepare({ path: 'new', content: 'approved' }, f.context);
    await writeFile(path.join(f.root, 'new'), 'unapproved');
    const rePrepared = await write.prepare({ path: 'new', content: 'approved' }, f.context);
    assert.notEqual(rePrepared.fingerprint, prepared.fingerprint);
    await assert.rejects(prepared.execute(signal()), /变化/);
    assert.equal(await readFile(path.join(f.root, 'new'), 'utf8'), 'unapproved');
    const abort = new AbortController(); abort.abort();
    const cancelled = await write.prepare({ path: 'cancelled', content: 'no' }, f.context);
    await assert.rejects(cancelled.execute(abort.signal));
    await assert.rejects(access(path.join(f.root, 'cancelled')));
  } finally { await f.dispose(); await outside.dispose(); }
});

function cli(code: string, limits: { timeoutMs?: number; maxOutputBytes?: number } = {}) {
  // 测试宿主显式固定一段程序；实际产品不得注册通用脚本入口。
  return createCliCapability({ profiles: { fixture: { executable: process.execPath, validateArgs: argv => argv.length === 2 && argv[0] === '-e' && argv[1] === code, env: { EAGLE_TEST: 'host-only' } } }, ...limits });
}

test('CLI 仅接受宿主配置、固定参数；准备无执行副作用', linux, async () => {
  const f = await fixture();
  try {
    const code = 'require("node:fs").writeFileSync("marker", process.env.EAGLE_TEST);process.stdout.write(process.cwd());';
    const capability = cli(code);
    await assert.rejects(capability.prepare({ profile: 'unknown', argv: [] }, f.context));
    await assert.rejects(capability.prepare({ profile: 'fixture', argv: ['-e', 'unapproved'] }, f.context));
    await assert.rejects(capability.prepare({ profile: 'fixture', argv: ['-e', code], executable: '/bin/sh' }, f.context));
    const action = await capability.prepare({ profile: 'fixture', argv: ['-e', code] }, f.context);
    assert.deepEqual(action.effects, ['execute']);
    await assert.rejects(access(path.join(f.root, 'marker')));
    const output = await action.execute(signal());
    assert.equal(await action.verify(output, signal()), true);
    assert.equal(await action.verify({ ...output }, signal()), false);
    assert.equal(await readFile(path.join(f.root, 'marker'), 'utf8'), 'host-only');
    assert.equal((output.value as { stdout: string }).stdout, f.root);
    assert.equal(output.evidence[0].kind, 'process-exit');
  } finally { await f.dispose(); }
});

test('CLI 非零退出无法通过验证；输出、超时和取消都有界', linux, async () => {
  const f = await fixture();
  try {
    let code = 'process.exit(7)';
    let action = await cli(code).prepare({ profile: 'fixture', argv: ['-e', code] }, f.context);
    const output = await action.execute(signal());
    assert.equal(await action.verify(output, signal()), false);
    code = 'process.stdout.write("x".repeat(10000))';
    action = await cli(code, { maxOutputBytes: 64 }).prepare({ profile: 'fixture', argv: ['-e', code] }, f.context);
    await assert.rejects(action.execute(signal()), /输出/);
    code = 'setInterval(()=>{}, 1000)';
    action = await cli(code, { timeoutMs: 100 }).prepare({ profile: 'fixture', argv: ['-e', code] }, f.context);
    await assert.rejects(action.execute(signal()), /超时/);
    action = await cli(code).prepare({ profile: 'fixture', argv: ['-e', code] }, f.context);
    const abort = new AbortController();
    const running = action.execute(abort.signal);
    setTimeout(() => abort.abort(new Error('test-cancel')), 100);
    await assert.rejects(running, /test-cancel/);
  } finally { await f.dispose(); }
});

test('CLI 主进程退出后清理同组后代', linux, async () => {
  const f = await fixture();
  try {
    const childCode = 'setTimeout(()=>require("node:fs").writeFileSync("orphan-marker","bad"),400)';
    const code = `require('node:child_process').spawn(process.execPath,['-e',${JSON.stringify(childCode)}],{stdio:'ignore'}).unref()`;
    const action = await cli(code).prepare({ profile: 'fixture', argv: ['-e', code] }, f.context);
    const output = await action.execute(signal());
    assert.equal(await action.verify(output, signal()), true);
    await new Promise(resolve => setTimeout(resolve, 550));
    await assert.rejects(access(path.join(f.root, 'orphan-marker')));
  } finally { await f.dispose(); }
});
