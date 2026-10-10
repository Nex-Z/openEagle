# Default-off Electron session fixture

This is a bounded integration test path, not a production agent mode. Set `OPEN_EAGLE_V2_FIXTURE=1` when launching an unpackaged Electron app to select a scripted engine and fake desktop broker. The environment flag is absent/disabled by default; the existing Python/WebSocket path remains the default. Packaged apps do not enable this mode.

The main process creates a new `openeagle-v2-app-*` directory under the system temporary directory before app readiness. It skips settings migration, Python backend startup and quick-assistant registration. Conversations/settings written by the existing UI go only to this new profile. Test profiles are retained for inspection; they are distinct from per-task `openeagle-ui-fixture-*` execution directories, which cleanup removes. Fixture windows enforce webSecurity, do not register the legacy asset protocol, and reject local asset conversion and desktop IPC operations. The utility receives a small explicit environment without provider keys. The fake broker implements only a fixed simulated click; no actual desktop input, browser account, model call or real workspace is used.

## Scope and reuse

- Selectively reuses the old Node migration's self-contained preload framing/sequence checks and backend-socket adapter. Default WebSocket construction preserves this checkout's legacy behavior.
- Selectively reuses its desktop capability client contract against a fake transport; no Electron capability tickets or actual desktop authority are granted.
- Adapts the protocol's envelope shape and confirmation broker's pending/request/sink lifecycle. The fixture broker binds decisions to unique confirmation IDs and original conversation IDs; the UI retains original request/conversation attribution across tab switches.
- Does not import BackendApplication, its composition root, configuration, memory, scheduler, IM, durable recovery or the old Pi version. The existing new core Pi adapter remains independently tested; this UI path uses ScriptedEngine.

Each session retains at most 128 admitted request IDs; exact retries do not execute again. Reusing an ID with different payload is rejected. One task per conversation is admitted at a time; the UI also synchronously blocks repeated clicks while its fixture task is active. Retention is in-memory and lasts for the port session, not across app restarts. Cancellation targets the original request and invokes TaskHandle.cancel; result emission follows runtime cleanup. Normal port disconnect and app exit await utility cleanup. A 3-second shutdown timeout logs forced termination and must not be interpreted as successful cleanup. The fixture transport is not a replacement for the full old migration's durable MessagePort/recovery implementation.

## Manual run

With locked dependencies installed, compile without packaging:

```powershell
corepack pnpm@10.7.0 --dir packages/agent-core build
corepack pnpm@10.7.0 exec tsc -p tsconfig.electron.json
node scripts/ensure-cjs-package.cjs
corepack pnpm@10.7.0 exec vite --host 127.0.0.1 --port 1420 --strictPort
```

Keep Vite running. In a second terminal, set `$env:OPEN_EAGLE_V2_FIXTURE='1'` and launch the installed Electron executable with this repository as its argument. The yellow `V2 测试模式` notice identifies the mode. Send any text, then allow/reject the simulated tool or use `取消测试任务`. Unset the environment flag to restore the default startup path. No automatic fallback from a failed fixture to a real model/backend occurs.

## Automated UI check

With Vite and compiled outputs ready, run `corepack pnpm@10.7.0 v2:test:ui`. If Electron was not installed by lifecycle scripts, set `OPENEAGLE_ELECTRON` to a verified Electron executable. The check creates hidden windows and fresh profiles, asserts the flag label, asset protocol rejection, full correlated lifecycle, cross-conversation result routing, cancellation, and pending-task cleanup on port disconnect and window close. It does not launch the default real-data application.

Validated locally with Electron 42.2.0 from the existing cache, checked against the installed package's SHA256 (`6e034b748ad5ed9445bd3da4b7d0792ed49556774a541217b507c156b00dd69a`). All three UI assertions groups passed; no renderer exception or forced-cleanup timeout was observed. Independent review found and verified fixes for asset access, disconnected-port cleanup, socket close propagation, app-exit cleanup and confirmation attribution. UI screenshot capture of a hidden window was unreliable; functional checks use DOM and actual protocol events.

This fixture UI was published at 8b13077a233511cb4ed73ea8568fbeab4d739c14; both platforms passed in run 38072515137. The separate default-off Pi session now has its own [Chinese setup and verification guide](./PI_SESSION.md).
