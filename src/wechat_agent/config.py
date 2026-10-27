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
    # Names are only addressing forms: an explicit @ mention or the message
    # prefix.  Keep free-form trigger words separate so a normal answer that
    # happens to contain "AI" does not trigger the listener again.
    wechat_keywords: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["问AI", "小助手"]
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
    # The UI adapter can only see the currently rendered chat page. Keep a
    # small, bounded window of messages from this group for prompt context.
    wechat_context_messages: int = Field(default=12, ge=0, le=50)
    wechat_context_max_chars: int = Field(default=6000, ge=500, le=30_000)

    desktop_host: str = "127.0.0.1"
    desktop_port: int = Field(default=8765, ge=1, le=65535)
    desktop_token: str = ""

    # Public-platform browser connector.  It is deliberately opt-in: the
    # offline adapters remain the default until an operator explicitly turns
    # on a visible, persistent browser session on the desktop host.
    platform_browser_enabled: bool = False
    platform_browser_data_dir: Path = Path("browser-data")
    platform_browser_executable: str | None = None
    platform_browser_headless: bool = False
    douyin_account_id: str = "unbound"
    xiaohongshu_account_id: str = "unbound"
    # Entries are stable platform user IDs only.  Display names are not unique
    # and are never used as an authorization key.
    douyin_mention_allowlist: Annotated[list[str], NoDecode] = Field(default_factory=list)
    xiaohongshu_mention_allowlist: Annotated[list[str], NoDecode] = Field(default_factory=list)
    platform_browser_scroll_steps: int = Field(default=5, ge=1, le=20)
    platform_browser_page_timeout_ms: int = Field(default=20_000, ge=5_000, le=120_000)
    platform_write_enabled: bool = False
    platform_action_delay_seconds: float = Field(default=30.0, ge=0, le=86_400)
    platform_max_actions_per_hour: int = Field(default=5, ge=1, le=100)

    # Optional online research. Search and fetch are separate switches. An
    # empty fetch-domain list allows public HTTP(S) hosts; setting the list
    # narrows retrieval to those domains. Local and private address targets
    # remain blocked by WebResearchClient.
    web_search_enabled: bool = False
    web_search_provider: Literal[
        "none",
        "brave",
        "tavily",
        "searxng",
        "deepseek-search-api",
    ] = "none"
    web_search_api_key: str = ""
    web_search_base_url: str = ""
    web_search_max_results: int = Field(default=5, ge=1, le=10)
    web_fetch_enabled: bool = False
    web_fetch_allowed_domains: Annotated[list[str], NoDecode] = Field(default_factory=list)
    web_fetch_max_chars: int = Field(default=6000, ge=1000, le=20_000)
    web_timeout_seconds: float = Field(default=15.0, gt=0, le=60.0)

    database_url: str = "sqlite:///state/memory.db"
    raw_retention_days: int = Field(default=30, ge=1)
    chat_half_life_days: float = Field(default=14, gt=0)
    interest_half_life_days: float = Field(default=90, gt=0)
    fact_half_life_days: float = Field(default=365, gt=0)

    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    llm_timeout_seconds: float = Field(default=60.0, gt=0)
    llm_max_output_tokens: int = Field(default=500, ge=32, le=16_384)

    # Operator-provided speaking style.  The bundled default is the
    # dafeiyu-persona adaptation under personas/dafeiyu/; deployments can point
    # this at any Markdown persona file and its adjacent references/ folder.
    persona_enabled: bool = True
    persona_name: str = "大肥鱼"
    persona_file: Path = Path("personas/dafeiyu/SKILL.md")
    persona_source_url: str = "https://github.com/ERUIHNIYHBKBNF/dafeiyu-persona"
    persona_max_chars: int = Field(default=12_000, ge=500, le=50_000)

    # Optional second model endpoint.  It is kept separate from the text
    # model so a single agent can route image requests to an OpenAI-compatible
    # ``/v1/images/generations`` service without changing chat credentials.
    image_model_enabled: bool = False
    image_model_base_url: str = ""
    image_model_api_key: str = ""
    image_model: str = ""
    image_model_size: str = "1024x1024"
    image_model_response_format: Literal["url", "b64_json"] = "url"
    image_model_timeout_seconds: float = Field(default=120.0, gt=0, le=600.0)

    # Optional direct upload of generated images to Alibaba Cloud OSS. The
    # agent never sends generated bytes through another application gateway.
    image_upload_enabled: bool = False
    oss_region: str = "cn-shenzhen"
    oss_endpoint: str = "https://oss-cn-shenzhen.aliyuncs.com"
    oss_bucket: str = ""
    oss_access_key_id: str = ""
    oss_access_key_secret: str = ""
    oss_prefix: str = "wechat-agent-assets"
    oss_signed_url_ttl_seconds: int = Field(default=7200, ge=300, le=604_800)
    # Set this only when the bucket or CDN is intentionally public. If empty,
    # generated links are short-lived OSS presigned GET URLs.
    oss_public_base_url: str = ""
    image_upload_timeout_seconds: float = Field(default=120.0, gt=0, le=600.0)
    image_upload_max_bytes: int = Field(default=20_000_000, ge=1_000_000, le=100_000_000)
    binding_code_ttl_minutes: int = Field(default=10, ge=1, le=60)

    @field_validator(
        "wechat_ai_aliases",
        "wechat_keywords",
        "wechat_allowed_members",
        "douyin_mention_allowlist",
        "xiaohongshu_mention_allowlist",
        "web_fetch_allowed_domains",
        mode="before",
    )
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
