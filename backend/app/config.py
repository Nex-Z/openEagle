from __future__ import annotations

from pydantic import BaseModel, Field


class AgentConfig(BaseModel):
    provider: str = "mock"
    model_id: str = Field(default="gpt-5-mini", alias="modelId")
    api_key: str | None = Field(default=None, alias="apiKey")
    base_url: str | None = Field(default=None, alias="baseUrl")

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

    model_config = {
        "populate_by_name": True,
    }


def load_config() -> AppConfig:
    return AppConfig()
