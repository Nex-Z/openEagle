from __future__ import annotations

from pydantic import BaseModel, Field


class ToolConfig(BaseModel):
    id: str
    name: str
    description: str = ""
    command: str = ""
    enabled: bool = True


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


class SoloConfig(BaseModel):
    preferred_display_index: int = Field(default=1, alias="preferredDisplayIndex")

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

    model_config = {
        "populate_by_name": True,
    }


class AppConfig(BaseModel):
    agent: AgentConfig = AgentConfig()
    feishu: FeishuConfig = FeishuConfig()
    solo: SoloConfig = SoloConfig()
    tools: list[ToolConfig] = Field(default_factory=list)
    mcp: list[McpConfig] = Field(default_factory=list)
    skills: list[SkillConfig] = Field(default_factory=list)

    model_config = {
        "populate_by_name": True,
    }


def load_config() -> AppConfig:
    return AppConfig()
