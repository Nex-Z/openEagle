import type { Capability, Json } from '../contracts.js';

/** 未来平台 helper 必须提供语义观察、执行回执和独立状态校验；本版本没有实现。 */
export interface NativeDesktopHelper {
  readonly platform: 'windows-uia' | 'macos-accessibility';
  observe(signal: AbortSignal): Promise<{observationId: string; capturedAt: number; tree: Json}>;
  perform(input: {observationId: string; elementId: string; operation: string; value?: string}, signal: AbortSignal): Promise<Json>;
  close(): Promise<void>;
}
export function nativeDesktopCapabilities(): Capability[] {
  return (['windows_uia', 'macos_accessibility'] as const).map(name => ({
    name,
    description: name === 'windows_uia' ? 'Windows UIA 原生控件（尚未实现）' : 'macOS Accessibility 原生控件（尚未实现）',
    inputSchema: {type:'object', properties:{}, additionalProperties:false},
    availability: {supported:false, reason:'尚未接入原生 helper；Linux 测试不能证明此能力可用'},
    async prepare() { throw new Error('原生桌面适配器尚未实现'); },
  }));
}
