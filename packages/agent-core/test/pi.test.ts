import assert from 'node:assert/strict';
import test from 'node:test';
import { createAssistantMessageEventStream, type Api, type AssistantMessage, type Model, type TranscriptContext } from '@earendil-works/pi-ai';
import type { StreamFn } from '@earendil-works/pi-agent-core';
import type { ActionRecord, EngineSession } from '../src/contracts.js';
import { PiEngine } from '../src/engines/pi.js';

// 真实发布版 Pi 循环 + 假传输：不访问网络，不使用付费模型或凭据。
const fakeModel: Model<Api> = {
  id: 'offline-test', name: '离线假传输', api: 'openai-completions', provider: 'offline-test',
  baseUrl: 'https://invalid.example', reasoning: false, input: ['text'],
  contextWindow: 4096, maxTokens: 256,
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
};
function response(content: AssistantMessage['content'], stopReason: AssistantMessage['stopReason'] = 'stop'): AssistantMessage {
  return {
    role: 'assistant', content, stopReason, api: fakeModel.api, provider: fakeModel.provider,
    model: fakeModel.id, timestamp: Date.now(),
    usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
  };
}
function streamMessage(message: AssistantMessage) {
  const stream = createAssistantMessageEventStream();
  if (message.stopReason === 'error' || message.stopReason === 'aborted') {
    stream.push({ type: 'error', reason: message.stopReason, error: message });
  } else {
    stream.push({ type: 'done', reason: message.stopReason as 'stop' | 'toolUse', message });
  }
  return stream;
}
const toolCall = response([{ type: 'toolCall', id: 'call-1', name: 'notes_lookup', arguments: { query: '旅行' } }], 'toolUse');
function session(invoke: EngineSession['invoke'], signal = new AbortController().signal): EngineSession {
  return {
    context: { id: 'task-1', goal: '查找旅行清单并总结', workspace: '/tmp', facts: [] },
    tools: [{ name: 'notes_lookup', description: '查找个人笔记', inputSchema: {
      type: 'object', properties: { query: { type: 'string' } }, required: ['query'], additionalProperties: false,
    } }], signal, invoke,
  };
}
function record(status: ActionRecord['status'] = 'succeeded'): ActionRecord {
  return { id: 'action-1', taskId: 'task-1', capability: 'notes_lookup', args: { query: '旅行' },
    startedAt: '2026-10-10T00:00:00Z', finishedAt: '2026-10-10T00:00:01Z', status,
    ...(status === 'succeeded' ? { output: { value: '带雨伞', evidence: [{ kind: 'read', source: 'notes', value: '带雨伞' }] } } : { error: '宿主拒绝或执行失败' }),
  };
}

test('真实 Pi 工具循环：假传输收到宿主观察结果并生成最终文本', async () => {
  let requests = 0;
  let invocations = 0;
  const runSession = session(async (name, args) => {
    invocations += 1;
    assert.equal(name, 'notes_lookup');
    assert.deepEqual(args, { query: '旅行' });
    return record();
  });
  const streamFn: StreamFn = (model, context, options) => {
    assert.equal(model, fakeModel);
    assert.equal(options?.signal, runSession.signal);
    assert.equal(options?.apiKey, undefined);
    requests += 1;
    if (requests === 1) return streamMessage(toolCall);
    const observation = context.messages.find((message) => message.role === 'toolResult');
    assert.ok(observation && observation.role === 'toolResult');
    assert.equal(observation.isError, false);
    assert.deepEqual(JSON.parse(observation.content.find((part) => part.type === 'text')!.text), record());
    return streamMessage(response([{ type: 'text', text: '笔记里建议带雨伞。' }]));
  };
  assert.equal(await new PiEngine({ model: fakeModel, streamFn }).run(runSession), '笔记里建议带雨伞。');
  assert.equal(requests, 2);
  assert.equal(invocations, 1);
});

for (const status of ['denied', 'failed', 'unverified', 'timed-out', 'cancelled'] as const) {
  test(`宿主 ${status} 作为 Pi 错误观察值返回，不能伪装成功`, async () => {
    let requests = 0;
    const streamFn: StreamFn = (_model, context) => {
      if (++requests === 1) return streamMessage(toolCall);
      const observation = context.messages.find((message) => message.role === 'toolResult');
      assert.ok(observation && observation.role === 'toolResult' && observation.isError);
      assert.match(JSON.stringify(observation.content), new RegExp(status));
      return streamMessage(response([{ type: 'text', text: '该操作未成功。' }]));
    };
    await new PiEngine({ model: fakeModel, streamFn }).run(session(async () => record(status)));
  });
}

test('宿主抛出的工具异常由真实 Pi 循环转为错误观察值', async () => {
  let requests = 0;
  const streamFn: StreamFn = (_model, context) => {
    if (++requests === 1) return streamMessage(toolCall);
    const observation = context.messages.find((message) => message.role === 'toolResult');
    assert.ok(observation && observation.role === 'toolResult' && observation.isError);
    assert.match(JSON.stringify(observation.content), /工具异常/);
    return streamMessage(response([{ type: 'text', text: '工具执行失败。' }]));
  };
  await new PiEngine({ model: fakeModel, streamFn }).run(session(async () => { throw new Error('工具异常'); }));
});

test('重复工具调用在最大轮数停止，不隐式启动第二个循环', async () => {
  let requests = 0;
  let invocations = 0;
  const streamFn: StreamFn = () => { requests += 1; return streamMessage(toolCall); };
  await assert.rejects(new PiEngine({ model: fakeModel, streamFn, maxTurns: 2 }).run(session(async () => {
    invocations += 1;
    return record();
  })), /最大轮数 2/);
  assert.equal(requests, 2);
  assert.equal(invocations, 2);
});

test('取消在请求开始前阻止传输', async () => {
  const controller = new AbortController();
  controller.abort(new Error('已取消'));
  const streamFn: StreamFn = () => { throw new Error('不应调用'); };
  await assert.rejects(new PiEngine({ model: fakeModel, streamFn }).run(session(async () => record(), controller.signal)), /已取消/);
});

test('取消信号传至假传输，运行以取消失败而不是成功文本结束', async () => {
  const controller = new AbortController();
  const streamFn: StreamFn = (_model, _context, options) => {
    assert.equal(options?.signal, controller.signal);
    const stream = createAssistantMessageEventStream();
    options.signal!.addEventListener('abort', () => {
      const message = response([], 'aborted');
      message.errorMessage = '已取消';
      stream.push({ type: 'error', reason: 'aborted', error: message });
    }, { once: true });
    queueMicrotask(() => controller.abort(new Error('已取消')));
    return stream;
  };
  await assert.rejects(new PiEngine({ model: fakeModel, streamFn }).run(session(async () => record(), controller.signal)), /已取消/);
});

test('模型错误不返回看似成功的文本', async () => {
  const message = response([{ type: 'text', text: '不完整文本' }], 'error');
  message.errorMessage = '离线传输失败';
  await assert.rejects(new PiEngine({ model: fakeModel, streamFn: () => streamMessage(message) }).run(session(async () => record())), /离线传输失败/);
});

test('普通对话无需工具，模型宣称完成也不会产生宿主验证记录', async () => {
  let invocations = 0;
  let transcript: TranscriptContext | undefined;
  const streamFn: StreamFn = (_model, context) => {
    transcript = context;
    return streamMessage(response([{ type: 'text', text: '我已完成并验证所有操作。' }]));
  };
  const result = await new PiEngine({ model: fakeModel, streamFn, maxTurns: 1 }).run(session(async () => {
    invocations += 1;
    return record();
  }));
  assert.equal(result, '我已完成并验证所有操作。');
  assert.equal(invocations, 0);
  assert.match(JSON.stringify(transcript), /通用个人助手/);
});

test('无效轮数和缺失显式传输配置在运行前拒绝', () => {
  for (const maxTurns of [0, -1, NaN, Infinity, 1.5]) {
    assert.throws(() => new PiEngine({ model: fakeModel, streamFn: () => streamMessage(response([])), maxTurns }), /正整数/);
  }
  assert.throws(() => new PiEngine({ model: fakeModel } as never), /显式 model 和 streamFn/);
});

for (const [name, args] of [['unknown_tool', {}], ['notes_lookup', {}]] as const) {
  test(`Pi 拒绝未知工具或不合 schema 的参数：${name}`, async () => {
    let requests = 0;
    let invocations = 0;
    const streamFn: StreamFn = (_model, context) => {
      if (++requests === 1) return streamMessage(response([
        { type: 'toolCall', id: 'invalid-call', name, arguments: args },
      ], 'toolUse'));
      const observation = context.messages.find((message) => message.role === 'toolResult');
      assert.ok(observation && observation.role === 'toolResult' && observation.isError);
      return streamMessage(response([{ type: 'text', text: '工具调用被拒绝。' }]));
    };
    await new PiEngine({ model: fakeModel, streamFn }).run(session(async () => {
      invocations += 1;
      return record();
    }));
    assert.equal(invocations, 0);
  });
}

test('工具执行期间取消会阻止后续模型请求', async () => {
  const controller = new AbortController();
  let requests = 0;
  const streamFn: StreamFn = () => { requests += 1; return streamMessage(toolCall); };
  await assert.rejects(new PiEngine({ model: fakeModel, streamFn }).run(session(async () => {
    controller.abort(new Error('执行期间取消'));
    return record('cancelled');
  }, controller.signal)), /执行期间取消/);
  assert.equal(requests, 1);
});

test('真实 Pi 假传输指标记录模型调用、独立model span和usage而不记录内容',async()=>{
  const events: import('../src/metrics.js').MetricInput[]=[];
  const s=session(async()=>record());s.metric=e=>events.push(e);
  const engine=new PiEngine({model:fakeModel,streamFn:()=>streamMessage(response([{type:'text',text:'fixture'}]))});
  await engine.run(s);
  assert.equal(events.filter(e=>e.kind==='model-call').length,1);
  assert.equal(events.filter(e=>e.kind==='span-start'&&e.phase==='model').length,1);
  assert.equal(events.filter(e=>e.kind==='span-end'&&e.phase==='model').length,1);
  assert.equal(events.filter(e=>e.kind==='usage').length,1);
  assert(!JSON.stringify(events).includes('fixture'));
});
