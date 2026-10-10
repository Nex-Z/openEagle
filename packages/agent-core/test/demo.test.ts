import test from 'node:test';
import assert from 'node:assert/strict';
import { runDemo } from '../src/demo.js';
test('脚本规划 + 真实浏览器/文件/CLI：独立服务端回执与文件摘要闭环', {skip:!['linux','win32'].includes(process.platform)}, async () => {
  const result=await runDemo({...(process.platform === 'win32' ? {windowsMode:'trusted-workspace' as const} : {}),chromiumExecutable:process.env.OPENEAGLE_CHROMIUM});
  assert.equal(result.submitted,true); assert.equal(result.result.records.length,8);
  assert.equal(result.sharedEvidenceCount,8); assert.match(result.sha256,/^[a-f0-9]{64}$/);
});
