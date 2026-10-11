import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';

test('renderer transport defaults to legacy WebSocket; fixture closes real port', () => {
  const source=readFileSync(new URL('../../../../src/lib/backend-socket.ts',import.meta.url),'utf8');
  const javascript=ts.transpileModule(source,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS}}).outputText;
  class FakeWebSocket { static CONNECTING=0;static OPEN=1;static CLOSED=3;constructor(readonly url:string){} }
  let closed=0;let listener:((event:{kind:string;data?:string})=>void)|undefined;
  const window:{electronAPI?:unknown}={};
  const exports:Record<string,unknown>={};
  vm.runInNewContext(javascript,{exports,window,WebSocket:FakeWebSocket});
  const create=exports.createBackendSocket as (port:number)=>{url?:string;readyState:number;send(data:string):void;close():void};
  assert.equal(create(8123).url,'ws://127.0.0.1:8123/ws');
  window.electronAPI={fixtureMode:true,backendPort:{onEvent:(f:typeof listener)=>{listener=f;return()=>{};},send:()=>true,close:()=>closed++}};
  const socket=create(1);listener!({kind:'open'});assert.equal(socket.readyState,1);
  socket.send('{}');socket.close();socket.close();assert.equal(closed,1);
  assert.throws(()=>socket.send('{}'),/not writable/);
});
