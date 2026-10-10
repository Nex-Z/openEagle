import test from 'node:test';
import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';
import { runDemo } from '../src/demo.js';
test('脚本规划 + 真实浏览器/文件/CLI：独立服务端回执与文件摘要闭环', {skip:process.platform!=='linux'}, async () => {
  const result=await runDemo({chromiumExecutable:process.env.OPENEAGLE_CHROMIUM ?? (existsSync('/usr/bin/chromium') ? '/usr/bin/chromium' : undefined)});
  assert.equal(result.submitted,true); assert.equal(result.result.records.length,8);
  assert.equal(result.sharedEvidenceCount,8); assert.match(result.sha256,/^[a-f0-9]{64}$/);
});
