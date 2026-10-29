from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from pydantic import AliasChoices, Field, field_validator, model_validator
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
    # ``WECHAT_GROUP_NAMES`` is the multi-group setting.  The singular field
    # remains as a compatibility input for existing .env files and is kept in
    # sync with the first configured group after validation.
    wechat_group_name: str = ""
    wechat_group_names: Annotated[list[str], NoDecode] = Field(default_factory=list)
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
    # Explicit proactive participation.  It never fires unless enabled, and
    # only for a bounded set of social cues; the default stays off so a normal
    # group message is still skipped unless it addresses the bot.
    wechat_proactive_reply: bool = False
    wechat_proactive_min_interval_seconds: float = Field(default=300.0, ge=0.0, le=86_400.0)
    wechat_proactive_max_per_hour: int = Field(default=6, ge=0, le=100)
    wechat_proactive_probability: float = Field(default=0.4, ge=0.0, le=1.0)
    wechat_poll_seconds: float = Field(default=2.0, ge=0.5, le=60.0)
    # Messages that arrive while a reply is still being generated wait in a
    # bounded FIFO backlog and are answered one per poll cycle.  ``0`` keeps the
    # backlog unbounded; overflow drops the oldest pending message.
    wechat_reply_queue_max_size: int = Field(default=50, ge=0, le=1_000)
    # Reply frequency controls are deliberately disabled by default.  They
    # limit successful outbound replies after a trigger has already matched;
    # they do not turn ordinary group messages into model requests.
    wechat_reply_cooldown_seconds: float = Field(default=0.0, ge=0.0, le=86_400.0)
    wechat_max_replies_per_hour: int = Field(default=0, ge=0, le=1_000)
    wechat_stop_file: Path = Path("state/STOP")
    wechat_processing_lease_seconds: int = Field(default=300, ge=30, le=86400)
    wechat_send_retry_count: int = Field(default=3, ge=1, le=5)
    wechat_send_verify_seconds: float = Field(default=15.0, gt=1.0, le=120.0)
    wechat_skip_existing_on_start: bool = True
    # The UI adapter can only see the currently rendered chat page. Keep a
    # small, bounded window of messages from the current group for prompt context.
    wechat_context_messages: int = Field(default=12, ge=0, le=50)
    wechat_context_max_chars: int = Field(default=6000, ge=500, le=30_000)
    # Image rows are downloaded from the local WeChat attachment store before
    # being sent as bounded data URLs to a vision-capable OpenAI-compatible
    # model. Recognition is opt-in at runtime and never uploads raw paths.
    wechat_image_recognition_enabled: bool = True
    wechat_image_max_count: int = Field(default=4, ge=0, le=12)
    wechat_image_max_bytes: int = Field(default=12_000_000, ge=100_000, le=100_000_000)
    # A picture stays prompt-visible for a few messages after it arrives, so a
    # follow-up question can still refer to it.  Older pictures only come back
    # when somebody explicitly mentions the image again.
    wechat_image_context_turns: int = Field(default=4, ge=0, le=50)
    wechat_image_reference_window: int = Field(default=30, ge=1, le=200)
    wechat_media_dir: Path = Path("state/wechat-media")
    # Generated pictures (image-model output) default next to the chat media so
    # deployments can keep every runtime file on one data disk.
    image_output_dir: Path = Path("state/generated")

    # The group style layer is a bounded, aggregate profile derived from
    # ordinary member messages.  It is kept per account + group and is only
    # refreshed after enough new messages arrive, so every reply does not
    # trigger another model call.
    group_style_enabled: bool = True
    group_style_min_messages: int = Field(default=20, ge=5, le=500)
    group_style_refresh_messages: int = Field(default=20, ge=1, le=500)
    group_style_sample_messages: int = Field(default=80, ge=10, le=500)
    group_style_max_chars: int = Field(default=1800, ge=300, le=10_000)
    # Style and AI profiles are rebuilt from a bounded recent window.  A
    # lifetime accumulator cannot drift with the group and contradicts the
    # "最近 N 条" label it prints.
    group_style_window_messages: int = Field(default=300, ge=50, le=5_000)
    # Only the compact natural-language projection reaches the prompt; the
    # full audit profile stays in SQL/OpenViking.
    profile_prompt_max_chars: int = Field(default=4_000, ge=500, le=20_000)
    # Semantic group profile (culture/jokes/nicknames/reply examples) is
    # synthesized from a bounded transcript, not from per-message counters.
    profile_synthesis_enabled: bool = True
    profile_synthesis_messages: int = Field(default=120, ge=20, le=500)
    profile_synthesis_max_input_chars: int = Field(default=6_000, ge=1_000, le=20_000)
    # Optional nightly local export for the style/behavior dataset.  It only
    # reads SQL and writes under data/ (gitignored); it never trains weights.
    training_export_enabled: bool = False
    training_export_dir: str = "data/training/daily"
    training_export_hour: int = Field(default=23, ge=0, le=23)
    training_export_minute: int = Field(default=30, ge=0, le=59)
    training_export_context_messages: int = Field(default=6, ge=1, le=30)
    training_export_anonymize: bool = True

    # Semantic memory.  The project accepts the generic EMBED_* names and the
    # existing WorkHub names already present in the local .env.  Secrets are
    # read at runtime only and are never included in diagnostics or prompts.
    embedding_enabled: bool = True
    embed_base: str = Field(
        default="",
        validation_alias=AliasChoices("EMBED_BASE", "WORKHUB_EMBED_BASE"),
    )
    embed_model: str = Field(
        default="",
        validation_alias=AliasChoices("EMBED_MODEL", "WORKHUB_EMBED_MODEL"),
    )
    embed_key: str = Field(
        default="",
        validation_alias=AliasChoices("EMBED_KEY", "WORKHUB_EMBED_KEY"),
    )
    # 0 means accept the provider's documented dimension and still require
    # every vector in one call to have the same length.  Set a positive value
    # when an operator wants a strict dimension check.
    embed_dimension: int = Field(default=0, ge=0, le=8192)
    embed_timeout_seconds: float = Field(default=45.0, gt=0, le=300.0)

    # OpenViking is the preferred remote memory base when configured.  The
    # local SQL records remain the auditable source of truth and provide a
    # lexical/vector fallback when the service is unavailable.
    openviking_enabled: bool = True
    openviking_url: str = ""
    openviking_api_key: str = ""
    # The configured OpenViking API key belongs to the ``ds`` user.  Keep the
    # same user as the safe fallback so a missing .env value cannot silently
    # read or write the Codex ``default`` user space.
    openviking_namespace: str = "ds"
    openviking_timeout_seconds: float = Field(default=45.0, gt=0, le=300.0)
    openviking_connect_timeout_seconds: float = Field(default=5.0, gt=0, le=60.0)
    openviking_failure_cooldown_seconds: float = Field(default=300.0, ge=0.0, le=86_400.0)
    openviking_commit: bool = True
    # Profile mirrors are derived from SQL.  Coalesce writes per scope instead
    # of uploading a full profile for every incoming message.
    openviking_profile_min_interval_seconds: float = Field(default=30.0, ge=0.0, le=3_600.0)

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
    llm_max_output_tokens: int = Field(default=3000, ge=32, le=16_384)
    llm_temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    llm_top_p: float = Field(default=1.0, gt=0.0, le=1.0)
    llm_frequency_penalty: float = Field(default=0.0, ge=-2.0, le=2.0)
    llm_presence_penalty: float = Field(default=0.0, ge=-2.0, le=2.0)

    # Operator-provided speaking style.  The bundled default is the
    # dafeiyu-persona adaptation under personas/dafeiyu/; deployments can point
    # this at any Markdown persona file and its adjacent references/ folder.
    persona_enabled: bool = True
    persona_name: str = "大肥鱼"
    persona_file: Path = Path("personas/dafeiyu/SKILL.md")
    persona_source_url: str = "https://github.com/ERUIHNIYHBKBNF/dafeiyu-persona"
    persona_max_chars: int = Field(default=12_000, ge=500, le=50_000)
    # Deterministic post-checks keep the persona human-like: no self-AI
    # phrasing, no bubble gimmicks and only a small number of emoji.
    persona_style_filter: bool = True
    persona_max_emoji: int = Field(default=1, ge=0, le=5)

    # Optional second model endpoint.  It is kept separate from the text
    # model so a single agent can route image requests to an OpenAI-compatible
    # ``/v1/images/generations`` service without changing chat credentials.
    image_model_enabled: bool = False
    image_model_base_url: str = ""
    image_model_api_key: str = ""
    image_model: str = ""
    image_model_size: str = "1024x1024"
    image_model_response_format: Literal["auto", "url", "b64_json"] = "auto"
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
        "wechat_group_names",
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

    @model_validator(mode="after")
    def normalize_group_names(self) -> "Settings":
        """Accept both the new list setting and the old singular setting.

        A comma-separated value is deliberately parsed here instead of relying
        on pydantic-settings JSON decoding, so the Web UI and hand-edited
        ``.env`` files can use the same simple ``群A,群B`` format.
        """

        groups: list[str] = []
        # The new plural setting wins when present. This prevents a stale
        # WECHAT_GROUP_NAME line in an older .env from silently adding an
        # unintended listener after the operator switches to the Web UI.
        source_values = self.wechat_group_names or [self.wechat_group_name]
        for value in source_values:
            for item in str(value or "").split(","):
                item = item.strip()
                if item and item not in groups:
                    groups.append(item)
        self.wechat_group_names = groups
        self.wechat_group_name = groups[0] if groups else ""
        return self

    @property
    def stop_file(self) -> Path:
        return self.wechat_stop_file

    def half_life_for(self, memory_type: Literal["chat", "interest", "fact"]) -> float:
        return {
            "chat": self.chat_half_life_days,
            "interest": self.interest_half_life_days,
            "fact": self.fact_half_life_days,
        }[memory_type]
