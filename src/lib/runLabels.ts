export function executionStateLabel(state: string) {
  return (
    {
      idle: "空闲",
      running: "运行中",
      paused: "已暂停",
      waiting_user_confirmation: "待确认",
      completed: "已完成",
      aborted: "已结束",
      error: "失败",
    }[state] ?? state
  );
}

export function executionStatusLabel(state: string) {
  return `桌面执行：${executionStateLabel(state)}`;
}
