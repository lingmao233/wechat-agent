from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-backed settings.

    Sending is opt-in. A group name is also required before the live adapter can
    open a conversation, which prevents an accidental broadcast to the current
    Weixin account.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    wechat_window_class: str = "mmui::MainWindow"
    wechat_exe: str | None = None
    wechat_group_name: str = ""
    wechat_ai_aliases: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["AI", "ai", "机器人"]
    )
    wechat_self_name: str = ""
    wechat_allowed_members: Annotated[list[str], NoDecode] = Field(default_factory=list)
    wechat_allow_send: bool = False
    # Kept as a config field for old .env files, but ordinary messages never
    # enter the reply path.  A message must explicitly address an AI alias.
    wechat_proactive_reply: bool = False
    wechat_poll_seconds: float = Field(default=2.0, ge=0.5, le=60.0)
    wechat_stop_file: Path = Path("state/STOP")
    wechat_processing_lease_seconds: int = Field(default=300, ge=30, le=86400)
    wechat_skip_existing_on_start: bool = True

    desktop_host: str = "127.0.0.1"
    desktop_port: int = Field(default=8765, ge=1, le=65535)
    desktop_token: str = ""

    database_url: str = "sqlite:///state/memory.db"
    raw_retention_days: int = Field(default=30, ge=1)
    chat_half_life_days: float = Field(default=14, gt=0)
    interest_half_life_days: float = Field(default=90, gt=0)
    fact_half_life_days: float = Field(default=365, gt=0)

    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    llm_timeout_seconds: float = Field(default=60.0, gt=0)
    llm_max_output_tokens: int = Field(default=500, ge=32, le=4096)
    binding_code_ttl_minutes: int = Field(default=10, ge=1, le=60)

    @field_validator("wechat_ai_aliases", "wechat_allowed_members", mode="before")
    @classmethod
    def split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @property
    def stop_file(self) -> Path:
        return self.wechat_stop_file

    def half_life_for(self, memory_type: Literal["chat", "interest", "fact"]) -> float:
        return {
            "chat": self.chat_half_life_days,
            "interest": self.interest_half_life_days,
            "fact": self.fact_half_life_days,
        }[memory_type]
