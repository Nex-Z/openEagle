from __future__ import annotations

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


class SoloConfig(BaseModel):
    preferred_display_index: int = Field(default=1, alias="preferredDisplayIndex")

    model_config = {
        "populate_by_name": True,
    }


class PermissionConfig(BaseModel):
    mode: str = "default"


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


class ImProviderConfig(BaseModel):
    id: str = "feishu"
    type: str = "feishu"
    name: str = ""
    enabled: bool = False
    app_id: str | None = Field(default=None, alias="appId")
    app_secret: str | None = Field(default=None, alias="appSecret")
    bot_token: str | None = Field(default=None, alias="botToken")
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
    im: ImConfig = ImConfig()
    permissions: PermissionConfig = PermissionConfig()
    solo: SoloConfig = SoloConfig()
    tools: list[ToolConfig] = Field(default_factory=list)
    builtin_tools: list[BuiltinToolConfig] = Field(default_factory=list, alias="builtinTools")
    mcp: list[McpConfig] = Field(default_factory=list)
    skills: list[SkillConfig] = Field(default_factory=list)

    model_config = {
        "populate_by_name": True,
    }


def load_config() -> AppConfig:
    return AppConfig()
