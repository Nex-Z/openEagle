# openEagle
是一个通过视觉来帮助你做事的agent。

# ui布局
主要布局参考chatgpt web端，主要部分是chat界面，左侧是历史会话、设置等等

# 项目架构设计
tarui2作为客户端技术栈 用于界面设计，对话，配置，交互入口。
python+fastapi作为后端，使用claude-agent-sdk-python
前后端交互通过websocket通信

## 打包分发
参考 `docs/架构设计理念.md`

# 通信方式
1、通过主页面对话框输入
2、通过接入飞书机器人（在设置里面配置接入）
