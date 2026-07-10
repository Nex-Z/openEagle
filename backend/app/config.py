from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ToolConfig(BaseModel):
    id: str
    name: str
    description: str = ""
    command: str = ""
    cwd: str = ""
    timeout_ms: int = Field(default=30_000, alias="timeoutMs")
    tail: int = 120
    enabled: bool = True

    model_config = {
        "populate_by_name": True,
    }


class McpConfig(BaseModel):
    id: str
    name: str
    transport: str = "stdio"
    endpoint: str = ""
    description: str = ""
    enabled: bool = True


class SkillConfig(BaseModel):
    id: str
    name: str
    description: str = ""
    prompt: str = ""
    enabled: bool = True


class BuiltinToolConfig(BaseModel):
    id: str
    name: str
    description: str = ""
    enabled: bool = True

    model_config = {
        "populate_by_name": True,
    }


class WebSearchConfig(BaseModel):
    provider: Literal["tavily", "disabled"] = "tavily"
    api_key: str | None = Field(default=None, alias="apiKey")
    search_depth: Literal["basic", "advanced"] = Field(
        default="basic",
        alias="searchDepth",
    )
    max_results: int = Field(default=5, ge=1, le=20, alias="maxResults")

    model_config = {
        "populate_by_name": True,
    }


class VoiceInputConfig(BaseModel):
    enabled: bool = False
    api_key: str | None = Field(default=None, alias="apiKey")
    base_url: str = Field(
        default="https://dashscope.aliyuncs.com/compatible-mode/v1",
        alias="baseUrl",
    )
    model_id: str = Field(default="qwen3-asr-flash", alias="modelId")
    max_duration_seconds: int = Field(
        default=120,
        ge=10,
        le=300,
        alias="maxDurationSeconds",
    )

    model_config = {
        "populate_by_name": True,
    }


class SoloConfig(BaseModel):
    preferred_display_index: int = Field(default=1, alias="preferredDisplayIndex")

    model_config = {
        "populate_by_name": True,
    }


class QuickAssistantConfig(BaseModel):
    enabled: bool = True
    hotkey: str = "Control+Alt+Space"
    auto_read_selection: bool = Field(default=True, alias="autoReadSelection")

    model_config = {
        "populate_by_name": True,
    }


class PermissionConfig(BaseModel):
    mode: str = "default"


class ContextConfig(BaseModel):
    enabled: bool = True
    max_input_tokens: int = Field(default=24_000, alias="maxInputTokens")
    conversation_turn_limit: int = Field(
        default=30,
        ge=1,
        le=200,
        alias="conversationTurnLimit",
    )
    preserve_recent_messages: int = Field(default=8, alias="preserveRecentMessages")
    preserve_first_messages: int = Field(default=2, ge=0, alias="preserveFirstMessages")
    im_idle_cleanup_minutes: int = Field(default=60, alias="imIdleCleanupMinutes")
    tool_message_mode: Literal["placeholder", "remove"] = Field(
        default="placeholder",
        alias="toolMessageMode",
    )
    ai_summary_enabled: bool = Field(default=True, alias="aiSummaryEnabled")
    snapshot_on_compaction: bool = Field(default=True, alias="snapshotOnCompaction")
    summary_char_limit: int = Field(default=2400, alias="summaryCharLimit")
    tool_result_char_limit: int = Field(default=0, alias="toolResultCharLimit")
    middle_message_char_limit: int = Field(default=1200, alias="middleMessageCharLimit")

    model_config = {
        "populate_by_name": True,
    }


class AgentConfig(BaseModel):
    provider: str = "mock"
    model_id: str = Field(default="gpt-5-mini", alias="modelId")
    api_key: str | None = Field(default=None, alias="apiKey")
    base_url: str | None = Field(default=None, alias="baseUrl")
    vl_provider: str = Field(default="openai", alias="vlProvider")
    vl_model_id: str = Field(default="gpt-4.1-mini", alias="vlModelId")
    vl_api_key: str | None = Field(default=None, alias="vlApiKey")
    vl_base_url: str | None = Field(default=None, alias="vlBaseUrl")

    model_config = {
        "populate_by_name": True,
    }


class FeishuConfig(BaseModel):
    enabled: bool = False
    app_id: str | None = Field(default=None, alias="appId")
    app_secret: str | None = Field(default=None, alias="appSecret")
    verification_token: str | None = Field(default=None, alias="verificationToken")
    allowed_open_ids: list[str] = Field(default_factory=list, alias="allowedOpenIds")
    allowed_chat_ids: list[str] = Field(default_factory=list, alias="allowedChatIds")

    model_config = {
        "populate_by_name": True,
    }


class TelegramConfig(BaseModel):
    enabled: bool = False
    bot_token: str | None = Field(default=None, alias="botToken")
    webhook_url: str | None = Field(default=None, alias="webhookUrl")
    allowed_user_ids: list[str] = Field(default_factory=list, alias="allowedUserIds")
    allowed_chat_ids: list[str] = Field(default_factory=list, alias="allowedChatIds")

    model_config = {
        "populate_by_name": True,
    }


class WechatConfig(BaseModel):
    enabled: bool = False
    account_id: str | None = Field(default=None, alias="accountId")
    base_url: str | None = Field(default=None, alias="baseUrl")
    bot_type: str = Field(default="3", alias="botType")
    allowed_user_ids: list[str] = Field(default_factory=list, alias="allowedUserIds")
    allowed_chat_ids: list[str] = Field(default_factory=list, alias="allowedChatIds")

    model_config = {
        "populate_by_name": True,
    }


class ImProviderConfig(BaseModel):
    id: str = "feishu"
    type: str = "feishu"
    name: str = ""
    enabled: bool = False
    app_id: str | None = Field(default=None, alias="appId")
    app_secret: str | None = Field(default=None, alias="appSecret")
    bot_token: str | None = Field(default=None, alias="botToken")
    account_id: str | None = Field(default=None, alias="accountId")
    base_url: str | None = Field(default=None, alias="baseUrl")
    bot_type: str = Field(default="3", alias="botType")
    allowed_open_ids: list[str] = Field(default_factory=list, alias="allowedOpenIds")
    allowed_user_ids: list[str] = Field(default_factory=list, alias="allowedUserIds")
    allowed_chat_ids: list[str] = Field(default_factory=list, alias="allowedChatIds")

    model_config = {
        "populate_by_name": True,
    }


class ImConfig(BaseModel):
    providers: list[ImProviderConfig] = Field(default_factory=list)


class AppConfig(BaseModel):
    agent: AgentConfig = AgentConfig()
    feishu: FeishuConfig = FeishuConfig()
    telegram: TelegramConfig = TelegramConfig()
    wechat: WechatConfig = WechatConfig()
    im: ImConfig = ImConfig()
    permissions: PermissionConfig = PermissionConfig()
    context: ContextConfig = ContextConfig()
    solo: SoloConfig = SoloConfig()
    quick_assistant: QuickAssistantConfig = Field(
        default_factory=QuickAssistantConfig,
        alias="quickAssistant",
    )
    tools: list[ToolConfig] = Field(default_factory=list)
    builtin_tools: list[BuiltinToolConfig] = Field(default_factory=list, alias="builtinTools")
    web_search: WebSearchConfig = Field(default_factory=WebSearchConfig, alias="webSearch")
    voice_input: VoiceInputConfig = Field(default_factory=VoiceInputConfig, alias="voiceInput")
    mcp: list[McpConfig] = Field(default_factory=list)
    skills: list[SkillConfig] = Field(default_factory=list)

    model_config = {
        "populate_by_name": True,
    }


def load_config() -> AppConfig:
    return AppConfig()
