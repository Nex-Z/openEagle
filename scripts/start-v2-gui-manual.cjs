// 一次性人工前台验收入口。只运行一个自有窗口；不循环重试或强制聚焦。
process.env.GUI_MANUAL_APPROVAL='1';
process.env.GUI_SCENARIOS=process.env.GUI_SCENARIOS||'success';
if(process.env.GUI_SCENARIOS.includes(','))throw new Error('手动入口一次仅运行一个场景');
require('./verify-v2-gui.cjs');
