from __future__ import annotations

import json
import logging
import threading
import webbrowser
from collections import deque
from dataclasses import asdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from .agent import WeChatAgent
from .config import Settings
from .platforms.browser import BrowserPlatformAdapter
from .platforms.dry_run import DryRunPlatformAdapter
from .platforms.service import PlatformService
from .storage import Storage


CONFIG_SCHEMA: list[dict[str, object]] = [
    # key, env, label, category, type, secret, description
    {"key": "wechat_window_class", "env": "WECHAT_WINDOW_CLASS", "label": "微信窗口类名", "category": "微信", "type": "text", "description": "通常保持 mmui::MainWindow。"},
    {"key": "wechat_exe", "env": "WECHAT_EXE", "label": "微信程序路径", "category": "微信", "type": "text", "description": "可留空，让程序自动寻找已运行的微信。"},
    {"key": "wechat_group_names", "env": "WECHAT_GROUP_NAMES", "label": "回复群聊（多个）", "category": "微信", "type": "text", "description": "逗号分隔多个群名或群 wxid；程序会同时监听这些群。"},
    {"key": "wechat_group_name", "env": "WECHAT_GROUP_NAME", "label": "旧版单群配置", "category": "微信", "type": "text", "description": "兼容旧 .env；新配置请使用上面的多个群设置。", "hidden": True},
    {"key": "wechat_ai_aliases", "env": "WECHAT_AI_ALIASES", "label": "怎么称呼机器人", "category": "微信", "type": "text", "description": "逗号分隔；消息以这些名称开头或明确 @ 机器人时才回复。"},
    {"key": "wechat_keywords", "env": "WECHAT_KEYWORDS", "label": "触发词", "category": "微信", "type": "text", "description": "逗号分隔；消息里出现这些词时进入回复。"},
    {"key": "wechat_self_name", "env": "WECHAT_SELF_NAME", "label": "机器人微信昵称", "category": "微信", "type": "text", "description": "用于识别 @ 机器人；不知道时可留空。"},
    {"key": "wechat_allowed_members", "env": "WECHAT_ALLOWED_MEMBERS", "label": "群成员白名单 ID", "category": "微信", "type": "text", "description": "逗号分隔，只填微信唯一 member_id/wxid；留空表示不额外限制成员。"},
    {"key": "wechat_allow_send", "env": "WECHAT_ALLOW_SEND", "label": "允许发出回复", "category": "微信", "type": "checkbox", "description": "关闭时只读取和记录，不会向微信发消息。"},
    {"key": "wechat_proactive_reply", "env": "WECHAT_PROACTIVE_REPLY", "label": "适度主动发言", "category": "微信", "type": "checkbox", "description": "不是每条都回；只在安慰、接梗、明显提问等场合择机插话，默认关闭。"},
    {"key": "wechat_proactive_min_interval_seconds", "env": "WECHAT_PROACTIVE_MIN_INTERVAL_SECONDS", "label": "主动发言最短间隔（秒）", "category": "微信", "type": "number", "description": "两次主动发言之间至少间隔多久；默认 300 秒。"},
    {"key": "wechat_proactive_max_per_hour", "env": "WECHAT_PROACTIVE_MAX_PER_HOUR", "label": "主动发言每小时上限", "category": "微信", "type": "number", "description": "一小时内最多主动插话几次；默认 6 次。"},
    {"key": "wechat_proactive_probability", "env": "WECHAT_PROACTIVE_PROBABILITY", "label": "主动发言概率", "category": "微信", "type": "number", "description": "命中社交信号后仍按概率择机发言；0 表示不主动，1 表示满足条件就发。"},
    {"key": "wechat_poll_seconds", "env": "WECHAT_POLL_SECONDS", "label": "检查新消息间隔（秒）", "category": "微信", "type": "number", "description": "两次读取微信新消息之间的间隔。"},
    {"key": "wechat_reply_queue_max_size", "env": "WECHAT_REPLY_QUEUE_MAX_SIZE", "label": "回复队列上限", "category": "微信", "type": "number", "description": "同时来消息时按到达顺序排队，每轮只回一条；队满时丢最旧的一条。0 表示不限长度。"},
    {"key": "wechat_reply_cooldown_seconds", "env": "WECHAT_REPLY_COOLDOWN_SECONDS", "label": "两次回复最短间隔（秒）", "category": "微信", "type": "number", "description": "限制成功回复之间的最短时间；0 表示不额外限制。"},
    {"key": "wechat_max_replies_per_hour", "env": "WECHAT_MAX_REPLIES_PER_HOUR", "label": "每小时最多回复数", "category": "微信", "type": "number", "description": "限制一小时内成功回复的数量；0 表示不设上限。"},
    {"key": "wechat_stop_file", "env": "WECHAT_STOP_FILE", "label": "紧急停止文件", "category": "微信", "type": "text", "description": "存在此文件时会立即关闭自动回复。"},
    {"key": "wechat_processing_lease_seconds", "env": "WECHAT_PROCESSING_LEASE_SECONDS", "label": "处理租约（秒）", "category": "微信", "type": "number", "description": "防止同一问题被多个轮询线程重复处理。"},
    {"key": "wechat_send_retry_count", "env": "WECHAT_SEND_RETRY_COUNT", "label": "发送失败重试次数", "category": "微信", "type": "number", "description": "原生文字/图片发送未确认时的有界重试次数。"},
    {"key": "wechat_send_verify_seconds", "env": "WECHAT_SEND_VERIFY_SECONDS", "label": "发送回读等待（秒）", "category": "微信", "type": "number", "description": "旧 UIA 路径等待本地数据库回读确认的时间。"},
    {"key": "wechat_skip_existing_on_start", "env": "WECHAT_SKIP_EXISTING_ON_START", "label": "启动跳过旧消息", "category": "微信", "type": "checkbox", "description": "避免启动时重复处理已显示的消息。"},
    {"key": "wechat_context_messages", "env": "WECHAT_CONTEXT_MESSAGES", "label": "上下文消息数", "category": "微信", "type": "number", "description": "传给模型的同群最近消息数量。"},
    {"key": "wechat_context_max_chars", "env": "WECHAT_CONTEXT_MAX_CHARS", "label": "上下文最大字符数", "category": "微信", "type": "number", "description": "限制上下文长度，避免请求过大。"},
    {"key": "wechat_image_recognition_enabled", "env": "WECHAT_IMAGE_RECOGNITION_ENABLED", "label": "识别群聊图片", "category": "微信", "type": "checkbox", "description": "读取本地微信图片附件并交给支持视觉输入的模型理解。"},
    {"key": "wechat_image_max_count", "env": "WECHAT_IMAGE_MAX_COUNT", "label": "单次最多图片数", "category": "微信", "type": "number", "description": "限制一次模型请求附带的图片数量。"},
    {"key": "wechat_image_max_bytes", "env": "WECHAT_IMAGE_MAX_BYTES", "label": "单张图片大小上限", "category": "微信", "type": "number", "description": "超过上限的本地附件不会上传给模型。"},
    {"key": "wechat_image_context_turns", "env": "WECHAT_IMAGE_CONTEXT_TURNS", "label": "图片参与上下文的消息数", "category": "微信", "type": "number", "description": "一张图只在之后这么多条消息内参与回答；更早的图要等群友重新提到才会再带上。"},
    {"key": "wechat_image_reference_window", "env": "WECHAT_IMAGE_REFERENCE_WINDOW", "label": "重新提到图片的追溯范围", "category": "微信", "type": "number", "description": "群友说“上面那张图”时，最多往回找多少条消息里的图片。"},
    {"key": "wechat_media_dir", "env": "WECHAT_MEDIA_DIR", "label": "图片临时目录", "category": "微信", "type": "text", "description": "解密后的图片临时保存目录。"},
    {"key": "image_output_dir", "env": "IMAGE_OUTPUT_DIR", "label": "生成图片目录", "category": "微信", "type": "text", "description": "生图模型生成的文件保存目录。"},
    {"key": "group_style_enabled", "env": "GROUP_STYLE_ENABLED", "label": "自动拟合群聊风格", "category": "人设", "type": "checkbox", "description": "按当前群的普通消息统计表达习惯，作为动态语气参考。"},
    {"key": "group_style_min_messages", "env": "GROUP_STYLE_MIN_MESSAGES", "label": "开始拟合所需消息数", "category": "人设", "type": "number", "description": "达到这个数量后才生成第一版群聊风格画像。"},
    {"key": "group_style_refresh_messages", "env": "GROUP_STYLE_REFRESH_MESSAGES", "label": "画像刷新间隔消息数", "category": "人设", "type": "number", "description": "新增多少条普通消息后重新计算一次画像。"},
    {"key": "group_style_sample_messages", "env": "GROUP_STYLE_SAMPLE_MESSAGES", "label": "画像指纹保留数", "category": "人设", "type": "number", "description": "保留多少条事件指纹用于防止重放计数；不会保存群聊原文。"},
    {"key": "group_style_max_chars", "env": "GROUP_STYLE_MAX_CHARS", "label": "群聊画像最大字符数", "category": "人设", "type": "number", "description": "限制动态风格摘要注入模型的长度。"},
    {"key": "desktop_host", "env": "DESKTOP_HOST", "label": "UI 绑定地址", "category": "高级", "type": "text", "description": "本机使用 127.0.0.1；远程访问必须配令牌和反向代理。"},
    {"key": "desktop_port", "env": "DESKTOP_PORT", "label": "UI 端口", "category": "高级", "type": "number", "description": "默认 8765；修改后需重启 UI 进程。"},
    {"key": "desktop_token", "env": "DESKTOP_TOKEN", "label": "UI 访问令牌", "category": "高级", "type": "password", "secret": True, "description": "绑定到非本机地址时必须设置。留空保持原值。"},
    {"key": "platform_browser_enabled", "env": "PLATFORM_BROWSER_ENABLED", "label": "启用抖音/小红书浏览器", "category": "平台", "type": "checkbox", "description": "启用后会打开可见浏览器并保存登录态。"},
    {"key": "platform_browser_data_dir", "env": "PLATFORM_BROWSER_DATA_DIR", "label": "浏览器数据目录", "category": "平台", "type": "text", "description": "保存抖音和小红书扫码登录态。"},
    {"key": "platform_browser_executable", "env": "PLATFORM_BROWSER_EXECUTABLE", "label": "浏览器程序路径", "category": "平台", "type": "text", "description": "留空自动寻找 Chrome 或 Edge。"},
    {"key": "platform_browser_headless", "env": "PLATFORM_BROWSER_HEADLESS", "label": "无头浏览器", "category": "平台", "type": "checkbox", "description": "需要扫码登录时必须关闭。"},
    {"key": "douyin_account_id", "env": "DOUYIN_ACCOUNT_ID", "label": "抖音账号 ID", "category": "平台", "type": "text", "description": "本地审计标识，不是密码；登录态由浏览器保存。"},
    {"key": "xiaohongshu_account_id", "env": "XIAOHONGSHU_ACCOUNT_ID", "label": "小红书账号 ID", "category": "平台", "type": "text", "description": "本地审计标识，不是密码；登录态由浏览器保存。"},
    {"key": "douyin_mention_allowlist", "env": "DOUYIN_MENTION_ALLOWLIST", "label": "抖音 @ 白名单 ID", "category": "平台", "type": "text", "description": "逗号分隔，只填平台唯一用户 ID。"},
    {"key": "xiaohongshu_mention_allowlist", "env": "XIAOHONGSHU_MENTION_ALLOWLIST", "label": "小红书 @ 白名单 ID", "category": "平台", "type": "text", "description": "逗号分隔，只填平台唯一用户 ID。"},
    {"key": "platform_browser_scroll_steps", "env": "PLATFORM_BROWSER_SCROLL_STEPS", "label": "滚动步数", "category": "平台", "type": "number", "description": "每次读取平台内容的滚动次数。"},
    {"key": "platform_browser_page_timeout_ms", "env": "PLATFORM_BROWSER_PAGE_TIMEOUT_MS", "label": "页面超时（毫秒）", "category": "平台", "type": "number", "description": "平台页面加载和查找控件的超时时间。"},
    {"key": "platform_write_enabled", "env": "PLATFORM_WRITE_ENABLED", "label": "允许平台评论/回复", "category": "平台", "type": "checkbox", "description": "开启后仍需在平台动作台账中二次确认。"},
    {"key": "platform_action_delay_seconds", "env": "PLATFORM_ACTION_DELAY_SECONDS", "label": "平台动作间隔（秒）", "category": "平台", "type": "number", "description": "连续评论或回复之间的最小间隔。"},
    {"key": "platform_max_actions_per_hour", "env": "PLATFORM_MAX_ACTIONS_PER_HOUR", "label": "每小时平台动作上限", "category": "平台", "type": "number", "description": "限制评论和回复的发送频率。"},
    {"key": "web_search_enabled", "env": "WEB_SEARCH_ENABLED", "label": "启用联网搜索", "category": "联网", "type": "checkbox", "description": "群友明确提问时才进入搜索流程。"},
    {"key": "web_search_provider", "env": "WEB_SEARCH_PROVIDER", "label": "搜索服务", "category": "联网", "type": "select", "options": ["none", "deepseek-search-api", "brave", "tavily", "searxng"], "description": "可使用 DSH 的 deepseek-search-api 中转。"},
    {"key": "web_search_api_key", "env": "WEB_SEARCH_API_KEY", "label": "搜索 API Key", "category": "联网", "type": "password", "secret": True, "description": "留空保持原值；DSH 中转通常填 lan。"},
    {"key": "web_search_base_url", "env": "WEB_SEARCH_BASE_URL", "label": "搜索服务地址", "category": "联网", "type": "text", "description": "例如 http://192.168.20.38:8787/v1/tools。"},
    {"key": "web_search_max_results", "env": "WEB_SEARCH_MAX_RESULTS", "label": "搜索结果数", "category": "联网", "type": "number", "description": "每次搜索返回的最大结果数。"},
    {"key": "web_fetch_enabled", "env": "WEB_FETCH_ENABLED", "label": "启用网页抓取", "category": "联网", "type": "checkbox", "description": "抓取消息中的公开 HTTP(S) 页面正文。"},
    {"key": "web_fetch_allowed_domains", "env": "WEB_FETCH_ALLOWED_DOMAINS", "label": "抓取域名白名单", "category": "联网", "type": "text", "description": "留空允许公开网站；填写后只抓这些域名。"},
    {"key": "web_fetch_max_chars", "env": "WEB_FETCH_MAX_CHARS", "label": "网页最大字符数", "category": "联网", "type": "number", "description": "单页正文进入模型上下文的上限。"},
    {"key": "web_timeout_seconds", "env": "WEB_TIMEOUT_SECONDS", "label": "联网超时（秒）", "category": "联网", "type": "number", "description": "搜索和抓取的网络超时时间。"},
    {"key": "llm_base_url", "env": "LLM_BASE_URL", "label": "文字模型地址", "category": "模型", "type": "text", "description": "OpenAI 兼容的 /v1 地址。"},
    {"key": "llm_api_key", "env": "LLM_API_KEY", "label": "文字模型 Key", "category": "模型", "type": "password", "secret": True, "description": "留空保持原值。"},
    {"key": "llm_model", "env": "LLM_MODEL", "label": "文字模型", "category": "模型", "type": "text", "description": "例如 deepseek-chat。"},
    {"key": "llm_timeout_seconds", "env": "LLM_TIMEOUT_SECONDS", "label": "模型超时（秒）", "category": "模型", "type": "number", "description": "文字模型请求超时时间。"},
    {"key": "llm_max_output_tokens", "env": "LLM_MAX_OUTPUT_TOKENS", "label": "最大输出 token", "category": "模型", "type": "number", "description": "限制文字回答长度。"},
    {"key": "llm_temperature", "env": "LLM_TEMPERATURE", "label": "回答随机性（Temperature）", "category": "模型", "type": "number", "description": "越低越稳定，越高越有变化；通常填 0 到 2。"},
    {"key": "llm_top_p", "env": "LLM_TOP_P", "label": "采样范围（Top-P）", "category": "模型", "type": "number", "description": "限制候选词范围；1 表示使用完整范围。"},
    {"key": "llm_frequency_penalty", "env": "LLM_FREQUENCY_PENALTY", "label": "重复词惩罚", "category": "模型", "type": "number", "description": "提高后会减少重复用词；0 表示不额外惩罚。"},
    {"key": "llm_presence_penalty", "env": "LLM_PRESENCE_PENALTY", "label": "新话题倾向", "category": "模型", "type": "number", "description": "提高后更倾向引入新词；0 表示不额外调整。"},
    {"key": "embedding_enabled", "env": "EMBEDDING_ENABLED", "label": "启用 Embedding", "category": "记忆", "type": "checkbox", "description": "关闭后使用本地词法召回，不调用 Embedding 服务。"},
    {"key": "embed_base", "env": "EMBED_BASE", "label": "Embedding 地址", "category": "记忆", "type": "text", "description": "支持 Ark 多模态完整地址或标准 OpenAI /embeddings 根地址。"},
    {"key": "embed_model", "env": "EMBED_MODEL", "label": "Embedding 模型", "category": "记忆", "type": "text", "description": "例如 doubao-embedding-vision-251215。"},
    {"key": "embed_key", "env": "EMBED_KEY", "label": "Embedding Key", "category": "记忆", "type": "password", "secret": True, "description": "只保存在运行机器 .env。"},
    {"key": "embed_dimension", "env": "EMBED_DIMENSION", "label": "Embedding 维度", "category": "记忆", "type": "number", "description": "0 表示接受服务端维度；当前 Ark 模型返回 2048。"},
    {"key": "openviking_enabled", "env": "OPENVIKING_ENABLED", "label": "启用 OpenViking", "category": "记忆", "type": "checkbox", "description": "启用远程语义记忆索引；不可用时回退本地台账。"},
    {"key": "openviking_url", "env": "OPENVIKING_URL", "label": "OpenViking 地址", "category": "记忆", "type": "text", "description": "填写 OpenViking REST 服务根地址。"},
    {"key": "openviking_api_key", "env": "OPENVIKING_API_KEY", "label": "OpenViking Key", "category": "记忆", "type": "password", "secret": True, "description": "只保存在运行机器 .env。"},
    {"key": "openviking_namespace", "env": "OPENVIKING_NAMESPACE", "label": "OpenViking 命名空间", "category": "记忆", "type": "text", "description": "用于构造稳定的账号/群/成员/任务 URI。"},
    {"key": "openviking_timeout_seconds", "env": "OPENVIKING_TIMEOUT_SECONDS", "label": "OpenViking 总超时（秒）", "category": "记忆", "type": "number", "description": "远程记忆单次请求总时限。"},
    {"key": "openviking_connect_timeout_seconds", "env": "OPENVIKING_CONNECT_TIMEOUT_SECONDS", "label": "OpenViking 连接超时（秒）", "category": "记忆", "type": "number", "description": "连接不上远程服务时快速失败并使用本地回退。"},
    {"key": "openviking_failure_cooldown_seconds", "env": "OPENVIKING_FAILURE_COOLDOWN_SECONDS", "label": "OpenViking 失败冷却（秒）", "category": "记忆", "type": "number", "description": "连续失败后暂停远程请求，避免每条消息重复等待。"},
    {"key": "persona_enabled", "env": "PERSONA_ENABLED", "label": "启用人设", "category": "人设", "type": "checkbox", "description": "只改变语气，不改变事实、安全规则和群成员隔离。"},
    {"key": "persona_name", "env": "PERSONA_NAME", "label": "人设名称", "category": "人设", "type": "text", "description": "例如大肥鱼。"},
    {"key": "persona_file", "env": "PERSONA_FILE", "label": "人设文件", "category": "人设", "type": "text", "description": "Markdown 文件路径；同目录 references/ 下的 lines.md、lore.md 会自动读取。"},
    {"key": "persona_source_url", "env": "PERSONA_SOURCE_URL", "label": "人设来源", "category": "人设", "type": "text", "description": "仅用于记录来源，不会在运行时联网下载。"},
    {"key": "persona_max_chars", "env": "PERSONA_MAX_CHARS", "label": "人设最大字符数", "category": "人设", "type": "number", "description": "限制注入模型的人设和参考资料长度。"},
    {"key": "persona_style_filter", "env": "PERSONA_STYLE_FILTER", "label": "人设输出检查", "category": "人设", "type": "checkbox", "description": "拦截自称 AI、吐泡泡、过激脏话，并限制表情数量。"},
    {"key": "persona_max_emoji", "env": "PERSONA_MAX_EMOJI", "label": "每条最多表情数", "category": "人设", "type": "number", "description": "默认 1；设为 0 可完全不用表情。"},
    {"key": "training_export_enabled", "env": "TRAINING_EXPORT_ENABLED", "label": "每晚导出训练数据", "category": "训练", "type": "checkbox", "description": "到点后把当天本地群聊导出到 data/；只导出，不自动训练权重。"},
    {"key": "training_export_dir", "env": "TRAINING_EXPORT_DIR", "label": "每日导出目录", "category": "训练", "type": "text", "description": "每天会在此目录下新建 YYYY-MM-DD 子目录；默认 data/training/daily。"},
    {"key": "training_export_hour", "env": "TRAINING_EXPORT_HOUR", "label": "每日导出小时", "category": "训练", "type": "number", "description": "本地时间 0~23；默认 23。"},
    {"key": "training_export_minute", "env": "TRAINING_EXPORT_MINUTE", "label": "每日导出分钟", "category": "训练", "type": "number", "description": "本地时间 0~59；默认 30。"},
    {"key": "training_export_context_messages", "env": "TRAINING_EXPORT_CONTEXT_MESSAGES", "label": "导出上下文条数", "category": "训练", "type": "number", "description": "behavior_pairs 每条样本带多少条前文；默认 6。"},
    {"key": "training_export_anonymize", "env": "TRAINING_EXPORT_ANONYMIZE", "label": "导出匿名化成员名", "category": "训练", "type": "checkbox", "description": "只替换成员/群标签；消息正文仍可能含昵称，分享前需人工检查。"},
    {"key": "image_model_enabled", "env": "IMAGE_MODEL_ENABLED", "label": "启用生图模型", "category": "生图 / OSS", "type": "checkbox", "description": "明确要求生成图片时才路由到第二个模型。"},
    {"key": "image_model_base_url", "env": "IMAGE_MODEL_BASE_URL", "label": "生图模型地址", "category": "生图 / OSS", "type": "text", "description": "OpenAI 兼容的图片生成服务地址。"},
    {"key": "image_model_api_key", "env": "IMAGE_MODEL_API_KEY", "label": "生图模型 Key", "category": "生图 / OSS", "type": "password", "secret": True, "description": "留空保持原值。"},
    {"key": "image_model", "env": "IMAGE_MODEL", "label": "生图模型名", "category": "生图 / OSS", "type": "text", "description": "由你的图片服务提供。"},
    {"key": "image_model_size", "env": "IMAGE_MODEL_SIZE", "label": "图片尺寸", "category": "生图 / OSS", "type": "text", "description": "例如 1024x1024。"},
    {"key": "image_model_response_format", "env": "IMAGE_MODEL_RESPONSE_FORMAT", "label": "生图返回格式", "category": "生图 / OSS", "type": "select", "options": ["auto", "url", "b64_json"], "description": "auto 省略可选字段并接受 URL 或 base64，兼容 GPT-image 网关。"},
    {"key": "image_model_timeout_seconds", "env": "IMAGE_MODEL_TIMEOUT_SECONDS", "label": "生图超时（秒）", "category": "生图 / OSS", "type": "number", "description": "图片模型请求超时时间。"},
    {"key": "image_upload_enabled", "env": "IMAGE_UPLOAD_ENABLED", "label": "上传生图到 OSS", "category": "生图 / OSS", "type": "checkbox", "description": "开启后额外上传供其他客户端访问；失败仍会发送本地微信图片附件。"},
    {"key": "oss_region", "env": "OSS_REGION", "label": "OSS Region", "category": "生图 / OSS", "type": "text", "description": "例如 cn-shenzhen。"},
    {"key": "oss_endpoint", "env": "OSS_ENDPOINT", "label": "OSS Endpoint", "category": "生图 / OSS", "type": "text", "description": "例如 https://oss-cn-shenzhen.aliyuncs.com。"},
    {"key": "oss_bucket", "env": "OSS_BUCKET", "label": "OSS Bucket", "category": "生图 / OSS", "type": "text", "description": "上传目标 Bucket。"},
    {"key": "oss_access_key_id", "env": "OSS_ACCESS_KEY_ID", "label": "OSS AccessKey ID", "category": "生图 / OSS", "type": "password", "secret": True, "description": "留空保持原值。"},
    {"key": "oss_access_key_secret", "env": "OSS_ACCESS_KEY_SECRET", "label": "OSS AccessKey Secret", "category": "生图 / OSS", "type": "password", "secret": True, "description": "留空保持原值。"},
    {"key": "oss_prefix", "env": "OSS_PREFIX", "label": "OSS 对象前缀", "category": "生图 / OSS", "type": "text", "description": "生成图片保存的目录前缀。"},
    {"key": "oss_signed_url_ttl_seconds", "env": "OSS_SIGNED_URL_TTL_SECONDS", "label": "签名 URL 有效期（秒）", "category": "生图 / OSS", "type": "number", "description": "未配置公开域名时使用。"},
    {"key": "oss_public_base_url", "env": "OSS_PUBLIC_BASE_URL", "label": "OSS 公开域名", "category": "生图 / OSS", "type": "text", "description": "留空则返回签名 URL。"},
    {"key": "image_upload_timeout_seconds", "env": "IMAGE_UPLOAD_TIMEOUT_SECONDS", "label": "OSS 上传超时（秒）", "category": "生图 / OSS", "type": "number", "description": "生成图片上传 OSS 的超时时间。"},
    {"key": "image_upload_max_bytes", "env": "IMAGE_UPLOAD_MAX_BYTES", "label": "图片大小上限（字节）", "category": "生图 / OSS", "type": "number", "description": "防止异常大的图片占用带宽。"},
    {"key": "database_url", "env": "DATABASE_URL", "label": "数据库连接串", "category": "高级", "type": "text", "description": "默认使用 state/memory.db；共享部署可改为 PostgreSQL。"},
    {"key": "raw_retention_days", "env": "RAW_RETENTION_DAYS", "label": "原始消息保留天数", "category": "高级", "type": "number", "description": "原始消息过期清理周期。"},
    {"key": "chat_half_life_days", "env": "CHAT_HALF_LIFE_DAYS", "label": "聊天记忆半衰期（天）", "category": "高级", "type": "number", "description": "聊天事实召回的时间衰减。"},
    {"key": "interest_half_life_days", "env": "INTEREST_HALF_LIFE_DAYS", "label": "兴趣记忆半衰期（天）", "category": "高级", "type": "number", "description": "兴趣画像召回的时间衰减。"},
    {"key": "fact_half_life_days", "env": "FACT_HALF_LIFE_DAYS", "label": "事实记忆半衰期（天）", "category": "高级", "type": "number", "description": "稳定事实召回的时间衰减。"},
    {"key": "binding_code_ttl_minutes", "env": "BINDING_CODE_TTL_MINUTES", "label": "绑定码有效期（分钟）", "category": "高级", "type": "number", "description": "平台身份绑定确认码的有效期。"},
]


class DesktopController:
    """Small local control plane for the Windows connector.

    The dashboard deliberately controls the same guarded ``WeChatAgent`` used
    by the CLI. It does not expose credentials or provide a second sending
    path. A server deployment can put the core service behind its own proxy;
    the Windows UI connector remains the process that owns the Weixin session.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        agent_factory: Callable[[Settings], WeChatAgent] | None = None,
        config_path: Path | None = None,
    ):
        self.settings = settings
        self.config_path = (config_path or Path(".env")).expanduser()
        if not self.config_path.is_absolute():
            self.config_path = (Path.cwd() / self.config_path).resolve()
        self._agent_factory_override = agent_factory
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._agent: WeChatAgent | None = None
        self._phase = "stopped"
        self._last_error: str | None = None
        self._started_at: str | None = None
        self._events: deque[dict[str, object]] = deque(maxlen=300)
        self._configure_services()

    def _configure_services(self) -> None:
        """Build storage and platform adapters from the current settings."""
        settings = self.settings
        self._storage = Storage(settings)
        self._storage.init_schema()
        if settings.platform_browser_enabled:
            adapters = {
                "douyin": BrowserPlatformAdapter(
                    "douyin",
                    data_dir=settings.platform_browser_data_dir,
                    executable_path=settings.platform_browser_executable,
                    account_id=settings.douyin_account_id,
                    headless=settings.platform_browser_headless,
                    scroll_steps=settings.platform_browser_scroll_steps,
                    page_timeout_ms=settings.platform_browser_page_timeout_ms,
                    write_enabled=settings.platform_write_enabled,
                    action_delay_seconds=settings.platform_action_delay_seconds,
                    max_actions_per_hour=settings.platform_max_actions_per_hour,
                ),
                "xiaohongshu": BrowserPlatformAdapter(
                    "xiaohongshu",
                    data_dir=settings.platform_browser_data_dir,
                    executable_path=settings.platform_browser_executable,
                    account_id=settings.xiaohongshu_account_id,
                    headless=settings.platform_browser_headless,
                    scroll_steps=settings.platform_browser_scroll_steps,
                    page_timeout_ms=settings.platform_browser_page_timeout_ms,
                    write_enabled=settings.platform_write_enabled,
                    action_delay_seconds=settings.platform_action_delay_seconds,
                    max_actions_per_hour=settings.platform_max_actions_per_hour,
                ),
            }
        else:
            adapters = {
                "douyin": DryRunPlatformAdapter("douyin"),
                "xiaohongshu": DryRunPlatformAdapter("xiaohongshu"),
            }
        self._platforms = PlatformService(
            self._storage,
            adapters,
            mention_allowlists={
                "douyin": settings.douyin_mention_allowlist,
                "xiaohongshu": settings.xiaohongshu_mention_allowlist,
            },
        )
        if self._agent_factory_override is None:
            self._agent_factory = lambda current_settings: WeChatAgent(
                current_settings,
                platform_service=self._platforms,
            )
        else:
            self._agent_factory = self._agent_factory_override

    def add_event(self, message: str, *, level: str = "INFO", source: str = "desktop") -> None:
        with self._lock:
            self._events.append(
                {
                    "at": datetime.now(timezone.utc).isoformat(),
                    "level": level.upper(),
                    "source": source,
                    "message": message[:1000],
                }
            )

    def events(self, limit: int = 100) -> list[dict[str, object]]:
        with self._lock:
            items = list(self._events)
        return items[-max(1, min(limit, 300)) :][::-1]

    def start(self) -> dict[str, object]:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self.status()
            self.settings.stop_file.unlink(missing_ok=True)
            self._phase = "starting"
            self._last_error = None
            self._started_at = datetime.now(timezone.utc).isoformat()
            self._agent = None
            self._thread = threading.Thread(target=self._run_agent, name="wechat-agent", daemon=True)
            self._thread.start()
            self.add_event("已开启自动回复", source="control")
            return self.status()

    def stop(self, timeout: float = 8.0) -> dict[str, object]:
        self.settings.stop_file.parent.mkdir(parents=True, exist_ok=True)
        self.settings.stop_file.write_text("stop\n", encoding="utf-8")
        with self._lock:
            thread = self._thread
            if thread and thread.is_alive():
                self._phase = "stopping"
        if thread and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))
        with self._lock:
            if not self._thread or not self._thread.is_alive():
                if self._phase != "error":
                    self._phase = "stopped"
            self.add_event("已关闭自动回复", source="control")
            return self.status()

    def clear_stop(self) -> dict[str, object]:
        self.settings.stop_file.unlink(missing_ok=True)
        with self._lock:
            if self._phase == "stopping":
                self._phase = "stopped"
            self.add_event("已清除 STOP 文件", source="control")
        return self.status()

    def reload_settings(self, new_settings: Settings) -> dict[str, object]:
        """Apply validated settings and restart the listener if it was running."""
        with self._lock:
            was_running = bool(self._thread and self._thread.is_alive())
        if was_running:
            self.stop()
        self.close_platforms()
        with self._lock:
            self.settings = new_settings
            self._configure_services()
            self._last_error = None
            self.add_event("配置已校验并重载；平台连接器已按新配置初始化", source="config")
        if was_running:
            self.start()
        return {"restarted": was_running, **self.status()}

    def config_snapshot(self) -> dict[str, object]:
        values: dict[str, object] = {}
        secrets: dict[str, bool] = {}
        model = self.settings.model_dump()
        for field in CONFIG_SCHEMA:
            key = str(field["key"])
            env_name = str(field["env"])
            value = model.get(key, "")
            if isinstance(value, Path):
                value = str(value)
            elif isinstance(value, list):
                value = ",".join(str(item) for item in value)
            is_secret = bool(field.get("secret"))
            secrets[env_name] = is_secret and bool(str(value))
            values[env_name] = "" if is_secret else value
        return {
            "path": str(self.config_path),
            "fields": CONFIG_SCHEMA,
            "values": values,
            "configured_secrets": secrets,
        }

    def save_config(self, payload: dict[str, object]) -> dict[str, object]:
        raw_values = payload.get("values")
        if not isinstance(raw_values, dict):
            raise ValueError("config_values_must_be_object")
        allowed = {str(field["env"]): field for field in CONFIG_SCHEMA}
        unknown = sorted(str(key) for key in raw_values if str(key) not in allowed)
        if unknown:
            raise ValueError(f"unsupported_config_keys:{','.join(unknown)}")
        clear_secrets = {
            str(item)
            for item in (payload.get("clear_secrets") or [])
            if isinstance(item, str)
        }
        original = self.config_path.read_text(encoding="utf-8") if self.config_path.exists() else ""
        current = _parse_env_file(original)
        # Once the Web UI uses the plural setting, clear the legacy singleton
        # so an old .env line cannot keep an unintended group active (including
        # when the operator intentionally clears the group list).
        if "WECHAT_GROUP_NAMES" in raw_values and "WECHAT_GROUP_NAME" not in raw_values:
            current["WECHAT_GROUP_NAME"] = ""
        for env_name, value in raw_values.items():
            env_name = str(env_name)
            field = allowed[env_name]
            if bool(field.get("secret")) and not str(value or "").strip():
                if env_name not in clear_secrets:
                    continue
                value = ""
            if isinstance(value, bool):
                text = "true" if value else "false"
            elif value is None:
                text = ""
            else:
                text = str(value).strip()
            current[env_name] = text
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        updated = _render_env_file(original, current)
        self.config_path.write_text(updated, encoding="utf-8")
        try:
            new_settings = Settings(_env_file=self.config_path)
        except Exception as exc:
            self.config_path.write_text(original, encoding="utf-8")
            raise ValueError(f"配置校验失败: {str(exc)[:800]}") from exc
        old_host, old_port = self.settings.desktop_host, self.settings.desktop_port
        result = self.reload_settings(new_settings)
        result["config"] = self.config_snapshot()
        result["restart_required"] = (
            new_settings.desktop_host != old_host
            or new_settings.desktop_port != old_port
        )
        return result

    def live(self) -> dict[str, object]:
        group_ids = list(getattr(self.settings, "wechat_group_names", None) or [])
        group_styles: dict[str, object] = {}
        for group_id in group_ids:
            snapshot = self._storage.group_style_snapshot("wechat-test-account", group_id)
            snapshot["enabled"] = self.settings.group_style_enabled
            group_styles[group_id] = snapshot
        group_style = (
            group_styles[group_ids[0]]
            if group_ids
            else {"enabled": self.settings.group_style_enabled, "ready": False}
        )
        return {
            "status": self.status(),
            "events": self.events(120),
            "replies": self.replies(80),
            "audit": self.audits(80),
            "group_style": group_style,
            "group_styles": group_styles,
            "platforms": self.platform_status(),
            "platform_actions": self.platform_actions(80),
        }

    def status(self) -> dict[str, object]:
        with self._lock:
            thread_alive = bool(self._thread and self._thread.is_alive())
            agent_stats = asdict(self._agent.stats) if self._agent else {}
            phase = self._phase
            if phase == "starting" and thread_alive and self._agent:
                phase = "running"
            return {
                "phase": phase,
                "thread_alive": thread_alive,
                "started_at": self._started_at,
                "last_error": self._last_error,
                "group_configured": bool(getattr(self.settings, "wechat_group_names", None)),
                "group_name": ", ".join(getattr(self.settings, "wechat_group_names", None) or [self.settings.wechat_group_name]),
                "group_names": list(getattr(self.settings, "wechat_group_names", None) or []),
                "send_enabled": self.settings.wechat_allow_send,
                "proactive_enabled": self.settings.wechat_proactive_reply,
                "proactive_min_interval_seconds": self.settings.wechat_proactive_min_interval_seconds,
                "proactive_max_per_hour": self.settings.wechat_proactive_max_per_hour,
                "proactive_probability": self.settings.wechat_proactive_probability,
                "persona_style_filter": self.settings.persona_style_filter,
                "persona_max_emoji": self.settings.persona_max_emoji,
                "training_export_enabled": self.settings.training_export_enabled,
                "training_export_hour": self.settings.training_export_hour,
                "training_export_minute": self.settings.training_export_minute,
                "stop_file_present": self.settings.stop_file.exists(),
                "stats": agent_stats,
                "processing": self._storage.processing_summary(),
            }

    def audits(self, limit: int = 50) -> list[dict[str, object]]:
        return self._storage.recent_audits(limit)

    def replies(self, limit: int = 50) -> list[dict[str, object]]:
        return self._storage.recent_reply_records(limit)

    def platform_status(self) -> dict[str, dict[str, object]]:
        return self._platforms.login_status()

    def platform_actions(self, limit: int = 50) -> list[dict[str, object]]:
        return self._platforms.actions(limit)

    def open_platform(self, platform: str) -> dict[str, object]:
        return self._platforms.open(platform)  # type: ignore[arg-type]

    def close_platform(self, platform: str) -> dict[str, object]:
        return self._platforms.close(platform)  # type: ignore[arg-type]

    def browse_platform(self, platform: str, query: str = "", limit: int = 20) -> list[dict[str, object]]:
        return [
            asdict(item)
            for item in self._platforms.browse(platform, query=query, limit=limit)  # type: ignore[arg-type]
        ]

    def read_platform_comments(self, platform: str, item_id: str, limit: int = 50) -> list[dict[str, object]]:
        return [
            asdict(comment)
            for comment in self._platforms.read_comments(platform, item_id=item_id, limit=limit)  # type: ignore[arg-type]
        ]

    def plan_platform_action(self, payload: dict[str, object]) -> dict[str, object]:
        platform = str(payload.get("platform") or "")
        action_type = str(payload.get("action_type") or "")
        text = str(payload.get("text") or "")
        dry_run = bool(payload.get("dry_run", True))
        mention_ids = _payload_mention_ids(payload.get("mention_ids"))
        if action_type == "comment":
            receipt = self._platforms.plan_comment(
                platform,
                str(payload.get("item_id") or ""),
                text,
                mention_ids=mention_ids,
                dry_run=dry_run,  # type: ignore[arg-type]
            )
        elif action_type == "reply":
            receipt = self._platforms.plan_reply(
                platform,
                str(payload.get("comment_id") or ""),
                text,
                mention_ids=mention_ids,
                dry_run=dry_run,  # type: ignore[arg-type]
            )
        else:
            raise ValueError("unsupported_platform_action_type")
        return asdict(receipt)

    def execute_platform_action(self, payload: dict[str, object]) -> dict[str, object]:
        return self._platforms.execute(
            str(payload.get("action_key") or ""),
            confirm=bool(payload.get("confirm")),
        )

    def close_platforms(self) -> None:
        for platform in ("douyin", "xiaohongshu"):
            try:
                self._platforms.close(platform)  # type: ignore[arg-type]
            except Exception:
                pass

    def _run_agent(self) -> None:
        try:
            agent = self._agent_factory(self.settings)
            with self._lock:
                self._agent = agent
                self._phase = "running"
                self.add_event("自动回复已运行", source="agent")
            agent.run()
        except Exception as exc:  # surfaced in the dashboard for diagnosis
            with self._lock:
                self._last_error = f"{type(exc).__name__}: {str(exc)[:500]}"
                self._phase = "error"
                self.add_event(self._last_error, level="ERROR", source="agent")
        finally:
            with self._lock:
                if self._phase != "error":
                    self._phase = "stopped"
                    self.add_event("自动回复已停止", source="agent")


class DesktopServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self, server_address, handler, controller: DesktopController, token: str):
        super().__init__(server_address, handler)
        self.controller = controller
        self.token = token.strip()


class DesktopRequestHandler(BaseHTTPRequestHandler):
    server: DesktopServer

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        # The console has its own audit/error view; avoid duplicating request
        # text into stdout where it may contain chat content.
        return

    def do_GET(self) -> None:  # noqa: N802 - stdlib signature
        path = urlparse(self.path).path
        # Let a browser load the static UI so it can prompt for DESKTOP_TOKEN;
        # every API endpoint remains protected by the normal authorization gate.
        if path != "/" and not self._authorized():
            return
        if path == "/":
            self._send_html(DASHBOARD_HTML)
        elif path == "/api/status":
            self._send_json(self.server.controller.status())
        elif path == "/api/audit":
            self._send_json(self.server.controller.audits())
        elif path == "/api/replies":
            self._send_json(self.server.controller.replies())
        elif path == "/api/live":
            self._send_json(self.server.controller.live())
        elif path == "/api/events":
            self._send_json(self.server.controller.events())
        elif path == "/api/config":
            self._send_json(self.server.controller.config_snapshot())
        elif path == "/api/platforms":
            self._send_json(self.server.controller.platform_status())
        elif path == "/api/platform-actions":
            self._send_json(self.server.controller.platform_actions())
        else:
            self._send_json({"error": "not_found"}, 404)

    def do_POST(self) -> None:  # noqa: N802 - stdlib signature
        if not self._authorized():
            return
        path = urlparse(self.path).path
        try:
            body = self._read_json_body()
            if path == "/api/start":
                payload = self.server.controller.start()
            elif path == "/api/stop":
                payload = self.server.controller.stop()
            elif path == "/api/clear-stop":
                payload = self.server.controller.clear_stop()
            elif path == "/api/config":
                payload = self.server.controller.save_config(body)
                self.server.token = self.server.controller.settings.desktop_token.strip()
            elif path == "/api/platforms/open":
                payload = self.server.controller.open_platform(str(body.get("platform") or ""))
            elif path == "/api/platforms/close":
                payload = self.server.controller.close_platform(str(body.get("platform") or ""))
            elif path == "/api/platforms/browse":
                payload = self.server.controller.browse_platform(
                    str(body.get("platform") or ""),
                    query=str(body.get("query") or ""),
                    limit=int(body.get("limit", 20)),
                )
            elif path == "/api/platform-comments":
                payload = self.server.controller.read_platform_comments(
                    str(body.get("platform") or ""),
                    str(body.get("item_id") or ""),
                    limit=int(body.get("limit", 50)),
                )
            elif path == "/api/platform-actions/plan":
                payload = self.server.controller.plan_platform_action(body)
            elif path == "/api/platform-actions/execute":
                payload = self.server.controller.execute_platform_action(body)
            else:
                self._send_json({"error": "not_found"}, 404)
                return
        except (ValueError, KeyError) as exc:
            self._send_json({"error": str(exc)}, 400)
            return
        except Exception as exc:  # surfaced as a bounded connector error
            self._send_json({"error": f"{type(exc).__name__}: {str(exc)[:500]}"}, 409)
            return
        self._send_json(payload)

    def _read_json_body(self) -> dict[str, object]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(min(length, 1_000_000))
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid_json_body") from exc
        if not isinstance(decoded, dict):
            raise ValueError("json_body_must_be_object")
        return decoded

    def _authorized(self) -> bool:
        token = self.server.token
        if not token:
            return True
        supplied = self.headers.get("Authorization", "")
        if supplied.startswith("Bearer "):
            supplied = supplied.removeprefix("Bearer ").strip()
        supplied = supplied or self.headers.get("X-Desktop-Token", "")
        if supplied == token:
            return True
        self._send_json({"error": "unauthorized"}, 401)
        return False

    def _send_json(self, payload: object, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def _payload_mention_ids(value: object) -> list[str]:
    """Accept JSON arrays or a comma-separated operator input, never names."""
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, (list, tuple)):
        return [str(part).strip() for part in value if str(part).strip()]
    raise ValueError("mention_ids_must_be_array_or_csv")


def _parse_env_file(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[7:].lstrip()
        if "=" not in stripped:
            continue
        name, value = stripped.split("=", 1)
        name = name.strip()
        if not name or not name.replace("_", "").isalnum():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[name] = value
    return values


def _render_env_file(original: str, values: dict[str, str]) -> str:
    seen: set[str] = set()
    lines: list[str] = []
    for line in original.splitlines():
        stripped = line.strip()
        candidate = stripped[7:].lstrip() if stripped.startswith("export ") else stripped
        if "=" in candidate and not candidate.startswith("#"):
            name = candidate.split("=", 1)[0].strip()
            if name in values:
                prefix = "export " if stripped.startswith("export ") else ""
                lines.append(f"{prefix}{name}={_quote_env(values[name])}")
                seen.add(name)
                continue
        lines.append(line)
    missing = [name for name in values if name not in seen]
    if missing:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("# Managed by WeChat Agent desktop UI")
        lines.extend(f"{name}={_quote_env(values[name])}" for name in missing)
    return "\n".join(lines).rstrip() + "\n"


def _quote_env(value: str) -> str:
    value = str(value)
    if value and all(char.isalnum() or char in "._/:@%+,-" for char in value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\r", "\\r").replace("\n", "\\n")
    return f'"{escaped}"'


def create_server(
    settings: Settings,
    controller: DesktopController | None = None,
    *,
    config_path: Path | None = None,
) -> DesktopServer:
    host = settings.desktop_host.strip() or "127.0.0.1"
    if host not in {"127.0.0.1", "localhost", "::1"} and not settings.desktop_token.strip():
        raise ValueError("DESKTOP_TOKEN 必须在桌面控制台绑定非本机地址时设置")
    controller = controller or DesktopController(settings, config_path=config_path)
    return DesktopServer((host, settings.desktop_port), DesktopRequestHandler, controller, settings.desktop_token)


class DesktopLogHandler(logging.Handler):
    """Mirror agent logs into the UI's bounded real-time event feed."""

    def __init__(self, controller: DesktopController):
        super().__init__()
        self.controller = controller

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.controller.add_event(
                self.format(record),
                level=record.levelname,
                source=record.name or "log",
            )
        except Exception:
            # Logging must never break the agent or HTTP server.
            pass


def serve(
    settings: Settings,
    *,
    open_browser: bool = False,
    config_path: Path | None = None,
) -> None:
    server = create_server(settings, config_path=config_path)
    log_handler = DesktopLogHandler(server.controller)
    log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root_logger = logging.getLogger()
    root_logger.addHandler(log_handler)
    url = f"http://{settings.desktop_host}:{settings.desktop_port}/"
    print(f"wechat agent desktop UI: {url}")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.controller.stop()
        server.controller.close_platforms()
        root_logger.removeHandler(log_handler)
        server.server_close()
DASHBOARD_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>微信助手 · 群聊控制台</title>
  <style>
:root{
  --bg:#f6f6f4;
  --surface:#ffffff;
  --surface-soft:#fafaf8;
  --ink:#1f1f1d;
  --ink-soft:#4b4b47;
  --muted:#777772;
  --line:#e6e5e1;
  --line-strong:#d4d2cd;
  --accent:#1f2933;
  --accent-hover:#111820;
  --accent-soft:#f0f1ef;
  --green:#15803d;
  --green-soft:#eef8f1;
  --amber:#a16207;
  --amber-soft:#fbf6e8;
  --red:#b42318;
  --red-soft:#fff1ef;
  --radius:10px;
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{
  margin:0;
  background:var(--bg);
  color:var(--ink);
  font:14px/1.6 "Inter","Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  -webkit-font-smoothing:antialiased;
}
button,input,select,textarea{font:inherit}
button{color:inherit}
a{color:inherit}
.app{display:grid;grid-template-columns:216px minmax(0,1fr);min-height:100vh}
.sidebar{
  position:sticky;
  top:0;
  height:100vh;
  padding:24px 14px 18px;
  display:flex;
  flex-direction:column;
  background:#fbfbf9;
  border-right:1px solid var(--line);
}
.brand{display:flex;align-items:center;gap:10px;padding:0 8px 26px}
.brand-mark{
  width:30px;height:30px;display:grid;place-items:center;
  border-radius:7px;background:var(--accent);color:#fff;
  font-size:15px;font-weight:700;letter-spacing:-.02em;
}
.brand strong{display:block;font-size:14px;font-weight:650;line-height:1.3}
.brand small{display:block;margin-top:1px;color:var(--muted);font-size:11px}
.nav{display:grid;gap:2px}
.nav a{
  display:flex;align-items:center;gap:10px;
  padding:9px 10px;border-radius:6px;
  color:var(--muted);text-decoration:none;font-size:13px;
  transition:background .15s ease,color .15s ease;
}
.nav a:hover{background:var(--accent-soft);color:var(--ink)}
.nav a.active{background:var(--accent-soft);color:var(--ink);font-weight:600}
.nav-icon{width:17px;text-align:center;color:currentColor;font-size:14px;opacity:.8}
.sidebar-foot{
  display:flex;align-items:center;gap:8px;margin-top:auto;
  padding:14px 10px 2px;border-top:1px solid var(--line);
  color:var(--muted);font-size:11px;
}
.status-dot{width:7px;height:7px;border-radius:50%;background:#b8b6b1;flex:0 0 auto}
.status-dot.on{background:var(--green)}
.main{width:100%;max-width:1220px;padding:34px 40px 64px}
.page-header{display:flex;align-items:flex-end;justify-content:space-between;gap:20px;margin-bottom:30px}
.kicker{margin:0 0 7px;color:var(--muted);font-size:10px;font-weight:600;letter-spacing:.14em}
.page-header h1{margin:0;font-size:26px;line-height:1.2;font-weight:650;letter-spacing:-.025em}
.page-header p{margin:6px 0 0;color:var(--muted);font-size:13px}
.header-actions{display:flex;align-items:center;gap:12px}
.connection{display:flex;align-items:center;gap:7px;color:var(--muted);font-size:11px;white-space:nowrap}
.connection:before{content:"";width:6px;height:6px;border-radius:50%;background:#c4c1bb}
.connection.ok:before{background:var(--green)}
.connection.error{color:var(--red)}
.connection.error:before{background:var(--red)}
.btn{
  border:1px solid var(--accent);border-radius:6px;
  padding:8px 12px;background:var(--accent);color:#fff;
  font-size:12px;font-weight:600;cursor:pointer;
  transition:background .15s ease,border-color .15s ease;
}
.btn:hover{background:var(--accent-hover);border-color:var(--accent-hover)}
.btn.secondary{background:var(--surface);border-color:var(--line-strong);color:var(--ink-soft)}
.btn.secondary:hover{background:var(--surface-soft);border-color:#b9b6b0}
.btn.warn{background:var(--red);border-color:var(--red)}
.btn.warn:hover{background:#941b13;border-color:#941b13}
.btn:disabled{opacity:.5;cursor:wait}
.section{scroll-margin-top:22px;margin-top:36px}
.section-title{display:flex;align-items:flex-end;justify-content:space-between;gap:14px;margin-bottom:14px}
.section-title h2{margin:0;font-size:17px;font-weight:650;letter-spacing:-.01em}
.section-title p{margin:4px 0 0;color:var(--muted);font-size:12px}
.section-title .muted,.muted{color:var(--muted);font-size:11px}
.hero-grid{display:grid;grid-template-columns:minmax(0,1.25fr) minmax(320px,.75fr);gap:14px}
.hero-card,.quick-card,.panel,.settings-card,.advanced-card,.platform-shell,.style-profile-panel{
  background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);
}
.hero-card{padding:22px}
.hero-top{display:flex;justify-content:space-between;gap:20px}
.eyebrow{color:var(--muted);font-size:10px;font-weight:600;letter-spacing:.1em;text-transform:uppercase}
.hero-card h2{margin:6px 0 4px;font-size:24px;line-height:1.2;font-weight:650;letter-spacing:-.025em}
.hero-card p{margin:0;color:var(--muted);font-size:12px}
.hero-icon{
  width:38px;height:38px;display:grid;place-items:center;flex:0 0 auto;
  border:1px solid var(--line);border-radius:8px;
  background:var(--surface-soft);color:var(--ink-soft);font-size:17px;
}
.primary-control{
  display:flex;align-items:center;margin:22px 0 18px;padding:12px 13px;
  border:1px solid var(--line);border-radius:8px;background:var(--surface-soft);
}
.main-toggle{display:flex;align-items:center;gap:12px;border:0;background:transparent;padding:0;cursor:pointer;text-align:left}
.main-toggle .switch-ui{width:42px;height:24px}
.main-toggle .switch-ui:after{width:18px;height:18px;top:3px;left:3px}
.main-toggle input:checked+.switch-ui{background:var(--green)}
.main-toggle input:checked+.switch-ui:after{transform:translateX(18px)}
.toggle-copy b{display:block;font-size:13px;font-weight:600}
.toggle-copy small{display:block;margin-top:1px;color:var(--muted);font-size:11px}
.hero-meta{display:grid;grid-template-columns:1fr 1fr;gap:12px;padding-top:15px;border-top:1px solid var(--line)}
.hero-meta span{display:block;color:var(--muted);font-size:10px}
.hero-meta b{display:block;margin-top:3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px;font-weight:600}
.quick-card{padding:20px}
.card-heading{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}
.card-heading h2{margin:0;font-size:16px;font-weight:650}
.card-heading p{margin:4px 0 0;color:var(--muted);font-size:11px}
.tag{padding:3px 7px;border:1px solid var(--line);border-radius:99px;background:var(--surface-soft);color:var(--muted);font-size:10px;font-weight:500;white-space:nowrap}
.quick-list{display:grid;gap:0;margin-top:12px}
.quick-row{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:10px 0;border-bottom:1px solid var(--line);cursor:pointer}
.quick-row:last-child{border-bottom:0;padding-bottom:0}
.quick-row b{display:block;font-size:12px;font-weight:600}
.quick-row small{display:block;margin-top:1px;color:var(--muted);font-size:10.5px;line-height:1.45}
.switch-input{position:absolute;opacity:0;pointer-events:none}
.switch-ui{position:relative;display:block;flex:0 0 auto;width:38px;height:22px;border-radius:99px;background:#d6d3ce;transition:background .18s ease}
.switch-ui:after{content:"";position:absolute;top:3px;left:3px;width:16px;height:16px;border-radius:50%;background:#fff;box-shadow:0 1px 2px rgba(0,0,0,.12);transition:transform .18s ease}
.switch-input:checked+.switch-ui{background:var(--accent)}
.switch-input:checked+.switch-ui:after{transform:translateX(16px)}
.switch-input:disabled+.switch-ui{opacity:.45}
.stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin-top:14px}
.stat{padding:14px 15px;background:var(--surface);border:1px solid var(--line);border-radius:8px}
.stat-label{color:var(--muted);font-size:10px}
.stat-value{margin-top:3px;font-size:20px;font-weight:650;letter-spacing:-.02em;line-height:1.35}
.stat-hint{margin-top:2px;color:var(--muted);font-size:10px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.activity-grid{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.15fr);gap:14px}
.panel{padding:17px;min-width:0}
.panel-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px}
.panel h3{margin:0;font-size:13px;font-weight:650}
.panel-head small{color:var(--muted);font-size:10px}
.feed{display:grid;gap:6px;max-height:350px;overflow:auto}
.feed-row{padding:9px 10px;border:1px solid var(--line);border-radius:6px;background:var(--surface-soft)}
.feed-row .meta{display:flex;justify-content:space-between;gap:10px;color:var(--muted);font-size:9.5px}
.feed-row .body{margin-top:3px;white-space:pre-wrap;word-break:break-word;font-size:11.5px;line-height:1.55}
.table-wrap{overflow:auto;max-height:390px}
.data-table{width:100%;border-collapse:collapse;font-size:11.5px}
.data-table th,.data-table td{padding:8px 7px;text-align:left;vertical-align:top;border-bottom:1px solid var(--line)}
.data-table th{position:sticky;top:0;background:var(--surface);color:var(--muted);font-size:10px;font-weight:600;white-space:nowrap}
.badge{display:inline-flex;align-items:center;border-radius:99px;padding:2px 7px;background:var(--accent-soft);color:var(--ink-soft);font-size:9.5px;font-weight:500}
.badge.green{background:var(--green-soft);color:#166534}
.badge.red{background:var(--red-soft);color:#991b1b}
.badge.amber{background:var(--amber-soft);color:#854d0e}
.empty{padding:22px 4px;color:var(--muted);text-align:center;font-size:11px}
.audit-details{margin-top:14px;padding:0;overflow:hidden}
.audit-details summary{padding:15px 17px;cursor:pointer;font-weight:600;font-size:12px;list-style:none}
.audit-details summary::-webkit-details-marker{display:none}
.audit-details summary:after{content:"展开";float:right;color:var(--muted);font-size:10px;font-weight:400}
.audit-details[open] summary{border-bottom:1px solid var(--line)}
.audit-details[open] summary:after{content:"收起"}
.audit-details .table-wrap{padding:0 10px 10px}
.style-profile-panel{margin-top:14px;padding:17px}
.settings-card{padding:20px}
.settings-card+.settings-card{margin-top:12px}
.settings-group+.settings-group{margin-top:18px;padding-top:18px;border-top:1px solid var(--line)}
.settings-group h3{margin:0;font-size:14px;font-weight:650}
.settings-group>p{margin:4px 0 13px;color:var(--muted);font-size:11.5px}
.setting-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px 20px}
.setting-field{min-width:0}
.setting-field.full{grid-column:1/-1}
.setting-field label{display:block;margin-bottom:5px;font-size:11.5px;font-weight:600;color:var(--ink-soft)}
.setting-field input{width:100%;border:1px solid var(--line-strong);border-radius:6px;padding:8px 10px;background:var(--surface);color:var(--ink);outline:0;font-size:12.5px}
.setting-field input:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
.setting-field small{display:block;margin-top:4px;color:var(--muted);font-size:10.5px;line-height:1.5}
.settings-actions{display:flex;align-items:center;gap:12px;margin-top:17px;padding-top:15px;border-top:1px solid var(--line)}
.settings-actions span{color:var(--muted);font-size:10.5px}
.advanced-card{margin-top:12px;overflow:hidden}
.advanced-card summary{padding:15px 18px;cursor:pointer;list-style:none;font-weight:600;font-size:12px}
.advanced-card summary::-webkit-details-marker{display:none}
.advanced-card summary small{display:block;margin-top:2px;color:var(--muted);font-size:10.5px;font-weight:400}
.advanced-card summary:after{content:"展开";float:right;color:var(--muted);font-size:10px;font-weight:400}
.advanced-card[open] summary{border-bottom:1px solid var(--line)}
.advanced-card[open] summary:after{content:"收起"}
.advanced-body{padding:16px 18px 18px}
.tabs{display:flex;gap:3px;overflow:auto;padding-bottom:8px;margin-bottom:15px;border-bottom:1px solid var(--line)}
.tab{padding:6px 9px;border:0;border-radius:5px;background:transparent;color:var(--muted);cursor:pointer;white-space:nowrap;font-size:11px}
.tab:hover{background:var(--accent-soft)}
.tab.active{background:var(--accent-soft);color:var(--ink);font-weight:600}
.form-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px 20px}
.field{min-width:0}
.field.full{grid-column:1/-1}
.field label{display:flex;justify-content:space-between;gap:8px;margin-bottom:5px;font-size:11.5px;font-weight:600;color:var(--ink-soft)}
.field label span{color:var(--muted);font-size:9.5px;font-weight:400}
.field input,.field select{width:100%;border:1px solid var(--line-strong);border-radius:6px;padding:8px 10px;background:var(--surface);color:var(--ink);outline:0;font-size:12.5px}
.field input:focus,.field select:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
.field small{display:block;margin-top:4px;color:var(--muted);font-size:10.5px;line-height:1.5}
.secret-line{display:flex;align-items:center;gap:8px}
.secret-line input{flex:1}
.clear-secret{color:var(--muted);font-size:10.5px;white-space:nowrap}
.platform-shell{padding:18px}
.platform-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.platform-card{padding:14px;border:1px solid var(--line);border-radius:7px}
.platform-card h3{margin:0 0 3px;font-size:13px;font-weight:650}
.platform-card p{margin:0 0 11px;color:var(--muted);font-size:11px}
.platform-actions{display:flex;flex-wrap:wrap;gap:6px}
.platform-actions .btn{padding:7px 9px;font-size:10.5px}
.platform-output{
  margin-top:12px;max-height:220px;overflow:auto;padding:11px;
  border:1px solid var(--line);border-radius:6px;
  background:var(--surface-soft);color:var(--ink-soft);
  white-space:pre-wrap;word-break:break-word;
  font:10.5px/1.55 "SFMono-Regular",Consolas,"Liberation Mono",monospace;
}
.footer{margin-top:36px;padding-top:16px;border-top:1px solid var(--line);color:var(--muted);font-size:10.5px}
.toast{
  position:fixed;right:20px;bottom:20px;z-index:10;max-width:380px;
  padding:10px 13px;border-radius:7px;background:var(--ink);color:#fff;
  opacity:0;transform:translateY(8px);pointer-events:none;
  transition:opacity .18s ease,transform .18s ease;font-size:11.5px;
}
.toast.show{opacity:1;transform:none}
.toast.error{background:var(--red)}
/* Basic settings extras */
.switch-row{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:12px 0;border-top:1px solid var(--line);cursor:pointer}
.switch-row:first-of-type{border-top:0;padding-top:0}
.switch-row b{display:block;font-size:12px;font-weight:600}
.switch-row small{display:block;margin-top:2px;color:var(--muted);font-size:10.5px;line-height:1.5}
.switch-row .switch-ui{margin-top:2px}
.inline-fields{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin-top:12px}
.inline-fields .setting-field label{font-size:11px}
.settings-caption{margin:0 0 8px;color:var(--muted);font-size:11px;line-height:1.6}
.settings-caption strong{color:var(--ink-soft);font-weight:600}
@media(max-width:1080px){
  .app{grid-template-columns:188px minmax(0,1fr)}
  .sidebar{width:188px}
  .main{padding:28px 24px 56px}
  .hero-grid,.activity-grid{grid-template-columns:1fr}
  .stats{grid-template-columns:repeat(2,minmax(0,1fr))}
}
@media(max-width:700px){
  .app{display:block}
  .sidebar{position:static;width:100%;height:auto;padding:14px 16px;border-right:0;border-bottom:1px solid var(--line)}
  .brand{padding:0 4px 13px}
  .nav{display:flex;overflow:auto}
  .nav a{padding:8px 10px;white-space:nowrap}
  .sidebar-foot{display:none}
  .main{padding:22px 16px 44px}
  .page-header{display:block;margin-bottom:22px}
  .header-actions{margin-top:12px;justify-content:space-between}
  .page-header h1{font-size:23px}
  .hero-card,.quick-card,.panel,.settings-card,.advanced-body,.platform-shell,.style-profile-panel{padding:15px}
  .hero-card h2{font-size:21px}
  .stats,.setting-grid,.form-grid,.platform-grid,.inline-fields{grid-template-columns:1fr}
  .field.full,.setting-field.full{grid-column:auto}
  .section{margin-top:28px}
}
  </style>
</head>
<body>
  <div class="app">
    <aside class="sidebar">
      <div class="brand"><div class="brand-mark">鲸</div><div><strong>微信助手</strong><small>群聊控制台</small></div></div>
      <nav class="nav">
        <a class="active" href="#home"><span class="nav-icon">⌂</span>概览</a>
        <a href="#activity"><span class="nav-icon">◷</span>实时记录</a>
        <a href="#config"><span class="nav-icon">⚙</span>设置</a>
        <a href="#platforms"><span class="nav-icon">↗</span>平台工具</a>
      </nav>
      <div class="sidebar-foot"><i class="status-dot" id="sidebarDot"></i><span id="sidebarStatus">未运行</span></div>
    </aside>
    <main class="main">
      <header class="page-header">
        <div><p class="kicker">LOCAL CONSOLE</p><h1>微信助手</h1><p>群聊状态、发言节奏和本地数据，集中管理。</p></div>
        <div class="header-actions"><span class="connection" id="lastUpdate">正在连接</span><button class="btn secondary" onclick="refresh(true)">刷新</button></div>
      </header>

      <section id="home" class="section" style="margin-top:0">
        <div class="hero-grid">
          <article class="hero-card">
            <div class="hero-top"><div><span class="eyebrow">当前工作状态</span><h2 id="heroPhase">已关闭</h2><p id="heroHint">打开自动回复后，助手会处理指定群聊的新消息。</p></div><div class="hero-icon">✦</div></div>
            <div class="primary-control"><label class="main-toggle"><input id="agentToggle" class="switch-input" type="checkbox" onchange="toggleAgent(this)"><span class="switch-ui"></span><span class="toggle-copy"><b id="agentToggleLabel">开启自动回复</b><small id="agentToggleHint">目前关闭</small></span></label></div>
            <div class="hero-meta"><div><span>回复群聊</span><b id="groupSummary">尚未设置</b></div><div><span>发送模式</span><b id="sendSummary">只记录消息</b></div><div><span>主动发言</span><b id="proactiveSummary">未开启</b></div><div><span>每日导出</span><b id="trainingSummary">未开启</b></div></div>
          </article>
          <article class="quick-card"><div class="card-heading"><div><h2>常用开关</h2><p>点一下即可保存并重新加载。</p></div><span class="tag">快捷设置</span></div><div class="quick-list">
            <label class="quick-row"><div><b>允许发出回复</b><small>关闭时只记录消息，不会发到微信</small></div><input class="switch-input" type="checkbox" data-quick-env="WECHAT_ALLOW_SEND" onchange="toggleQuick(this)"><span class="switch-ui"></span></label>
            <label class="quick-row"><div><b>启动时跳过旧消息</b><small>避免打开助手后重复处理历史内容</small></div><input class="switch-input" type="checkbox" data-quick-env="WECHAT_SKIP_EXISTING_ON_START" onchange="toggleQuick(this)"><span class="switch-ui"></span></label>
            <label class="quick-row"><div><b>允许联网搜索</b><small>有人提问时才搜索公开资料</small></div><input class="switch-input" type="checkbox" data-quick-env="WEB_SEARCH_ENABLED" onchange="toggleQuick(this)"><span class="switch-ui"></span></label>
            <label class="quick-row"><div><b>允许生成图片</b><small>收到明确的生图请求时才使用</small></div><input class="switch-input" type="checkbox" data-quick-env="IMAGE_MODEL_ENABLED" onchange="toggleQuick(this)"><span class="switch-ui"></span></label>
            <label class="quick-row"><div><b>适度主动发言</b><small>只在安慰、接梗、提问等时机插话</small></div><input class="switch-input" type="checkbox" data-quick-env="WECHAT_PROACTIVE_REPLY" onchange="toggleQuick(this)"><span class="switch-ui"></span></label>
            <label class="quick-row"><div><b>每晚导出语料</b><small>只写入本地 data/，不会自动训练权重</small></div><input class="switch-input" type="checkbox" data-quick-env="TRAINING_EXPORT_ENABLED" onchange="toggleQuick(this)"><span class="switch-ui"></span></label>
          </div></article>
        </div>
        <div class="stats"><div class="stat"><div class="stat-label">当前状态</div><div class="stat-value" id="phase">已关闭</div><div class="stat-hint" id="phaseHint">等待操作</div></div><div class="stat"><div class="stat-label">发消息权限</div><div class="stat-value" id="sendState">只记录</div><div class="stat-hint">上方可直接切换</div></div><div class="stat"><div class="stat-label">已处理消息</div><div class="stat-value" id="seenCount">0</div><div class="stat-hint" id="groupState">未设置群聊</div></div><div class="stat"><div class="stat-label">已完成回复</div><div class="stat-value" id="replyCount">0</div><div class="stat-hint" id="errorState">暂无失败记录</div></div></div>
      </section>

      <section id="activity" class="section"><div class="section-title"><div><h2>实时记录</h2><p>这里会显示助手刚刚做了什么，页面每 2 秒更新一次。</p></div><span class="muted">自动更新</span></div><div class="activity-grid"><article class="panel"><div class="panel-head"><h3>运行动态</h3><small>最近 120 条</small></div><div class="feed" id="events"></div></article><article class="panel"><div class="panel-head"><h3>最近回复</h3><small>包含成功和跳过</small></div><div class="table-wrap"><table class="data-table"><thead><tr><th>成员 / 状态</th><th>问题</th><th>助手回复</th></tr></thead><tbody id="replies"></tbody></table></div></article></div><details class="panel audit-details"><summary>发送记录</summary><div class="table-wrap"><table class="data-table"><thead><tr><th>时间</th><th>动作</th><th>群 / 成员</th><th>结果</th><th>原因</th></tr></thead><tbody id="audit"></tbody></table></div></details><article class="panel style-profile-panel"><div class="panel-head"><h3>群聊风格画像</h3><small id="styleProfileMeta">等待消息样本</small></div><p class="muted" id="styleProfileHint">达到设定的消息数后，系统会自动根据本群表达习惯拟合语气参考。</p><pre class="platform-output" id="styleProfile">尚未生成画像</pre></article></section>

      <section id="config" class="section">
        <div class="section-title"><div><h2>设置</h2><p>常用参数放在前面；模型、平台和本地安全选项在“更多设置”。</p></div><span class="muted" id="configPath">配置读取中</span></div>
        <div class="settings-card">
          <div class="settings-group">
            <h3>群聊与回复</h3>
            <p>告诉助手要处理哪个群，以及什么情况下回应。</p>
            <div class="setting-grid">
              <div class="setting-field"><label for="basicGroups">回复群聊（可多个）</label><input id="basicGroups" type="text" placeholder="例如：测试群A, 测试群B"><small>用逗号分开群名或群 wxid，程序会同时监听；留空时无法开启自动回复。</small></div>
              <div class="setting-field"><label for="basicSelfName">机器人昵称（可选）</label><input id="basicSelfName" type="text" placeholder="例如：大肥鱼"><small>用于识别群消息里的 @。</small></div>
              <div class="setting-field"><label for="basicAliases">称呼机器人</label><input id="basicAliases" type="text" placeholder="AI, 小助手"><small>用逗号分开；消息以这些名称开头时回复。</small></div>
              <div class="setting-field"><label for="basicKeywords">触发词（可选）</label><input id="basicKeywords" type="text" placeholder="问AI, 帮我看看"><small>消息中出现这些词时进入回复。</small></div>
              <div class="setting-field"><label for="basicReplyCooldown">回复最短间隔（秒）</label><input id="basicReplyCooldown" type="number" min="0" max="86400" step="0.5" placeholder="0"><small>限制两次成功回复之间的时间；填 0 表示不额外限制。</small></div>
              <div class="setting-field"><label for="basicMaxReplies">每小时最多回复数</label><input id="basicMaxReplies" type="number" min="0" max="1000" step="1" placeholder="0"><small>填 0 表示不设上限；只统计实际发出的回复。</small></div>
              <div class="setting-field full"><label for="basicMembers">只回复这些成员（可选）</label><input id="basicMembers" type="text" placeholder="留空表示群内所有成员"><small>用逗号分开成员 ID；留空不会额外限制。</small></div>
            </div>
          </div>
          <div class="settings-group">
            <h3>主动发言</h3>
            <p>不是每条都回；只在安慰、接梗、明显提问等场合择机插话。</p>
            <label class="switch-row"><div><b>允许主动插话</b><small>默认关闭；开启后仍受下面的间隔、上限和概率限制。</small></div><input id="basicProactive" class="switch-input" type="checkbox"><span class="switch-ui"></span></label>
            <div class="inline-fields">
              <div class="setting-field"><label for="basicProactiveInterval">最短间隔（秒）</label><input id="basicProactiveInterval" type="number" min="0" max="86400" step="1" placeholder="300"><small>两次主动发言之间至少间隔多久。</small></div>
              <div class="setting-field"><label for="basicProactiveMax">每小时上限</label><input id="basicProactiveMax" type="number" min="0" max="1000" step="1" placeholder="6"><small>填 0 表示不设上限。</small></div>
              <div class="setting-field"><label for="basicProactiveProbability">触发概率</label><input id="basicProactiveProbability" type="number" min="0" max="1" step="0.05" placeholder="0.4"><small>0 表示不主动，1 表示满足条件就发。</small></div>
            </div>
          </div>
          <div class="settings-group">
            <h3>人设与本地数据</h3>
            <p>控制输出风格和每天的训练数据导出；不会把提示词或过滤规则发到群里。</p>
            <label class="switch-row"><div><b>输出人设检查</b><small>拦截自称 AI、泡泡口癖、过激脏话和规则自曝。</small></div><input id="basicPersonaFilter" class="switch-input" type="checkbox"><span class="switch-ui"></span></label>
            <div class="setting-grid">
              <div class="setting-field"><label for="basicPersonaMaxEmoji">每条最多表情数</label><input id="basicPersonaMaxEmoji" type="number" min="0" max="10" step="1" placeholder="1"><small>设为 0 可完全不用表情。</small></div>
            </div>
            <label class="switch-row"><div><b>每晚导出当天语料</b><small>只写入本地 data/，不会自动训练权重。</small></div><input id="basicTrainingExport" class="switch-input" type="checkbox"><span class="switch-ui"></span></label>
            <div class="inline-fields">
              <div class="setting-field"><label for="basicTrainingExportHour">导出小时</label><input id="basicTrainingExportHour" type="number" min="0" max="23" step="1" placeholder="23"><small>本地时间，0~23。</small></div>
              <div class="setting-field"><label for="basicTrainingExportMinute">导出分钟</label><input id="basicTrainingExportMinute" type="number" min="0" max="59" step="1" placeholder="30"><small>本地时间，0~59。</small></div>
            </div>
            <p class="settings-caption">默认导出到 <strong>data/training/daily/&lt;日期&gt;</strong>；目录、上下文条数和匿名化开关在“更多设置 → 训练”。</p>
          </div>
          <div class="settings-actions"><button type="button" class="btn" onclick="saveBasicConfig()">保存常用设置</button><span id="basicSaveHint">保存后会自动重新加载助手。</span></div>
        </div>
        <details class="advanced-card"><summary>更多设置<small>模型、联网、平台和本地安全参数</small></summary><div class="advanced-body"><div class="tabs" id="configTabs"></div><form id="configForm" class="form-grid"></form><div class="settings-actions"><button type="button" class="btn" onclick="saveConfig()">保存更多设置</button><span>密码和密钥只显示是否已配置。</span></div></div></details>
      </section>

      <section id="platforms" class="section"><div class="section-title"><div><h2>平台工具</h2><p>需要时再打开抖音或小红书；浏览器默认关闭。</p></div></div><div class="platform-shell"><div class="platform-grid"><div class="platform-card"><h3>抖音</h3><p id="douyinStatus">状态读取中</p><div class="platform-actions"><button class="btn secondary" onclick="platformOpen('douyin')">打开浏览器</button><button class="btn secondary" onclick="platformBrowse('douyin')">读取内容</button></div></div><div class="platform-card"><h3>小红书</h3><p id="xhsStatus">状态读取中</p><div class="platform-actions"><button class="btn secondary" onclick="platformOpen('xiaohongshu')">打开浏览器</button><button class="btn secondary" onclick="platformBrowse('xiaohongshu')">读取内容</button></div></div></div><div class="platform-actions" style="margin-top:15px"><button class="btn secondary" onclick="platformComments()">读取评论</button><button class="btn secondary" onclick="platformPlan('comment')">计划评论</button><button class="btn secondary" onclick="platformPlan('reply')">计划回复</button><button class="btn warn" onclick="platformExecute()">确认执行</button></div><details><summary style="margin-top:15px;cursor:pointer;color:var(--muted);font-size:12px">查看平台数据</summary><pre class="platform-output" id="platformItems">尚未读取内容</pre><pre class="platform-output" id="platformComments">尚未读取评论</pre><pre class="platform-output" id="platformActions">动作记录读取中</pre></details></div></section>
      <div class="footer">微信助手 · 群聊控制台 · 关闭页面不会关闭助手，请使用上方“自动回复”开关。</div>
    </main>
  </div>
  <div class="toast" id="toast"></div>
  <script>
    const BASIC_ENVS=new Set(['WECHAT_GROUP_NAMES','WECHAT_SELF_NAME','WECHAT_AI_ALIASES','WECHAT_KEYWORDS','WECHAT_ALLOWED_MEMBERS','WECHAT_REPLY_COOLDOWN_SECONDS','WECHAT_MAX_REPLIES_PER_HOUR','WECHAT_PROACTIVE_REPLY','WECHAT_PROACTIVE_MIN_INTERVAL_SECONDS','WECHAT_PROACTIVE_MAX_PER_HOUR','WECHAT_PROACTIVE_PROBABILITY','PERSONA_STYLE_FILTER','PERSONA_MAX_EMOJI','TRAINING_EXPORT_ENABLED','TRAINING_EXPORT_HOUR','TRAINING_EXPORT_MINUTE']);
    const state={lastItems:[],lastComments:[],config:null,status:null,groupStyle:null,polling:false};
    let desktopToken=sessionStorage.getItem('wechatAgentDesktopToken')||new URLSearchParams(location.search).get('token')||'';
    if(desktopToken)sessionStorage.setItem('wechatAgentDesktopToken',desktopToken);
    const $=id=>document.getElementById(id);
    function esc(value){return String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
    function boolValue(value){return value===true||String(value).toLowerCase()==='true';}
    function toast(message,error=false){const el=$('toast');el.textContent=message;el.className='toast show'+(error?' error':'');clearTimeout(window.__toast);window.__toast=setTimeout(()=>el.className='toast',3600);}
    async function request(path,options={},retried=false){const headers=Object.assign({},options.headers||{});if(desktopToken)headers.Authorization='Bearer '+desktopToken;const response=await fetch(path,Object.assign({},options,{headers}));let data={};try{data=await response.json()}catch(_){data={error:'服务返回了无效 JSON'}}if(response.status===401&&!retried){const token=prompt('请输入 DESKTOP_TOKEN（仅保存在当前浏览器会话）','');if(token){desktopToken=token;sessionStorage.setItem('wechatAgentDesktopToken',desktopToken);return request(path,options,true)}}if(!response.ok)throw new Error(data.error||('HTTP '+response.status));return data;}
    async function post(path,body={}){return request(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})}
    async function savePartial(values,clearSecrets=[]){const result=await post('/api/config',{values,clear_secrets:clearSecrets});if(values.DESKTOP_TOKEN){desktopToken=values.DESKTOP_TOKEN;sessionStorage.setItem('wechatAgentDesktopToken',desktopToken)}else if(clearSecrets.includes('DESKTOP_TOKEN')){desktopToken='';sessionStorage.removeItem('wechatAgentDesktopToken')}state.config=result.config;renderConfig(state.config);await refresh(true);return result;}
    async function toggleAgent(input){const target=input.checked?'/api/start':'/api/stop';input.disabled=true;try{await post(target);toast(input.checked?'自动回复已开启':'自动回复正在关闭');await refresh(true)}catch(e){input.checked=!input.checked;toast(e.message,true)}finally{input.disabled=false}}
    async function toggleQuick(input){const env=input.dataset.quickEnv;const checked=input.checked;input.disabled=true;try{await savePartial({[env]:checked});toast(checked?'已开启：'+quickLabel(env):'已关闭：'+quickLabel(env))}catch(e){input.checked=!checked;toast(e.message,true)}finally{input.disabled=false}}
    function quickLabel(env){return ({WECHAT_ALLOW_SEND:'允许发出回复',WECHAT_SKIP_EXISTING_ON_START:'启动时跳过旧消息',WEB_SEARCH_ENABLED:'联网搜索',IMAGE_MODEL_ENABLED:'生成图片',WECHAT_PROACTIVE_REPLY:'适度主动发言',TRAINING_EXPORT_ENABLED:'每晚导出语料'})[env]||env}
    function fmtTime(value){if(!value)return '';try{return new Date(value).toLocaleTimeString()}catch(_){return value}}
    function phaseText(phase){return ({running:'正在工作',starting:'正在启动',stopping:'正在关闭',stopped:'已关闭',error:'出现错误'})[phase]||phase||'未知'}
    function badge(status){const s=String(status||'');let cls='badge';if(['sent','allowed','running'].includes(s))cls+=' green';if(['failed','blocked','error','rejected'].includes(s))cls+=' red';if(['queued','processing','starting','stopping'].includes(s))cls+=' amber';return '<span class="'+cls+'">'+esc(s==='allowed'?'允许':s==='blocked'?'已拦截':s||'—')+'</span>'}
    function renderStatus(status){state.status=status;const phase=phaseText(status.phase);const running=!!status.thread_alive;const busy=['starting','stopping'].includes(status.phase);const group=status.group_name||'尚未设置';$('heroPhase').textContent=phase;$('phase').textContent=phase;$('phaseHint').textContent=status.last_error|| (status.stop_file_present?'正在关闭自动回复':(group==='尚未设置'?'先在设置里填写回复群聊':'群聊：'+group));$('heroHint').textContent=status.last_error|| (running?'正在处理「'+group+'」的新消息':'打开自动回复后，助手会处理指定群聊的新消息');$('agentToggle').checked=running||status.phase==='starting';$('agentToggle').disabled=busy;$('agentToggleLabel').textContent=running?'关闭自动回复':'开启自动回复';$('agentToggleHint').textContent=busy?'正在切换…':running?'目前已开启':'目前已关闭';$('groupSummary').textContent=group;$('sendSummary').textContent=status.send_enabled?'可发送回复':'只记录消息';const proactiveOn=!!status.proactive_enabled;const proactiveInterval=Number(status.proactive_min_interval_seconds||0);const proactiveCap=Number(status.proactive_max_per_hour||0);$('proactiveSummary').textContent=proactiveOn?((proactiveInterval?proactiveInterval+' 秒':'无冷却')+' · '+(proactiveCap?proactiveCap+' 次/时':'不限')):'未开启';const exportOn=!!status.training_export_enabled;const exportHour=String(status.training_export_hour??23).padStart(2,'0');const exportMinute=String(status.training_export_minute??30).padStart(2,'0');$('trainingSummary').textContent=exportOn?('每天 '+exportHour+':'+exportMinute):'未开启';$('sendState').textContent=status.send_enabled?'可发送':'只记录';$('sendState').style.color=status.send_enabled?'var(--green)':'var(--amber)';const stats=status.stats||{};$('seenCount').textContent=stats.seen||0;$('replyCount').textContent=stats.replied||0;$('groupState').textContent=status.stop_file_present?'已请求关闭':(running?'正在处理新消息':'等待开启');$('errorState').textContent=(stats.failed||0)?(stats.failed+' 次失败 · 去重 '+(stats.duplicate_questions||0)):'暂无失败记录';$('sidebarDot').className='status-dot '+(running?'on':'');$('sidebarStatus').textContent=running?'自动回复已开启':phase;$('lastUpdate').textContent='更新于 '+new Date().toLocaleTimeString();$('lastUpdate').className='connection ok';}
    function renderEvents(items){const el=$('events');if(!items.length){el.innerHTML='<div class="empty">暂无动态。打开自动回复后，这里会显示实时记录。</div>';return}el.innerHTML=items.map(item=>'<div class="feed-row"><div class="meta"><span>'+esc(item.source||'system')+' · '+esc(item.level||'INFO')+'</span><span>'+esc(fmtTime(item.at))+'</span></div><div class="body">'+esc(String(item.message||'').replaceAll('监听','自动回复'))+'</div></div>').join('');}
    function renderReplies(items){const el=$('replies');if(!items.length){el.innerHTML='<tr><td colspan="3" class="empty">暂无回复记录</td></tr>';return}el.innerHTML=items.map(item=>'<tr><td>'+esc(item.member_name||'未知')+'<br>'+badge(item.status)+'</td><td>'+esc(item.text||'')+'<br><small>'+esc(fmtTime(item.updated_at))+'</small></td><td>'+esc(item.reply_text||item.reason||'—')+'</td></tr>').join('');}
    function renderAudit(items){const el=$('audit');if(!items.length){el.innerHTML='<tr><td colspan="5" class="empty">暂无发送记录</td></tr>';return}el.innerHTML=items.map(item=>'<tr><td>'+esc(fmtTime(item.created_at))+'</td><td>'+esc(item.action_type||'')+'</td><td>'+esc((item.group_id||'')+' / '+(item.target_member_id||'未指定'))+'</td><td>'+badge(item.allowed?'allowed':'blocked')+'</td><td>'+esc(item.reason||'')+'</td></tr>').join('');}
    function renderGroupStyle(profile){state.groupStyle=profile||{};const count=Number(profile&&profile.observed_count||0);const disabled=profile&&profile.enabled===false;const ready=!!(profile&&profile.ready&&!disabled);$('styleProfileMeta').textContent=disabled?'已关闭':ready?('已根据 '+count+' 条消息刷新'):('已采集 '+count+' 条消息');$('styleProfileHint').textContent=disabled?'群聊风格拟合已关闭。':'只调整语气和表达方式，不改变安全规则、触发条件或工具权限。';$('styleProfile').textContent=disabled?'已关闭':ready?(profile.profile||'尚未生成画像'):'样本不足，继续收集普通群消息后自动生成。';}
    function renderPlatforms(platforms,actions){const d=platforms&&platforms.douyin||{};const x=platforms&&platforms.xiaohongshu||{};$('douyinStatus').textContent=(d.logged_in?'已登录':'未登录')+' · '+(d.mode||'预览模式');$('xhsStatus').textContent=(x.logged_in?'已登录':'未登录')+' · '+(x.mode||'预览模式');$('platformActions').textContent=JSON.stringify(actions||[],null,2);}
    async function refresh(showError=false){if(state.polling)return;state.polling=true;try{const live=await request('/api/live');renderStatus(live.status);renderEvents(live.events||[]);renderReplies(live.replies||[]);renderAudit(live.audit||[]);renderGroupStyle(live.group_style||{});renderPlatforms(live.platforms||{},live.platform_actions||[])}catch(e){$('lastUpdate').textContent='连接失败';$('lastUpdate').className='connection error';if(showError)toast(e.message,true)}finally{state.polling=false}}
    function setInputValue(id,value){const el=$(id);if(el)el.value=value??''}
    function renderConfig(data){state.config=data;$('configPath').textContent=data.path||'本地配置';const values=data.values||{};setInputValue('basicGroups',values.WECHAT_GROUP_NAMES||values.WECHAT_GROUP_NAME);setInputValue('basicSelfName',values.WECHAT_SELF_NAME);setInputValue('basicAliases',values.WECHAT_AI_ALIASES);setInputValue('basicKeywords',values.WECHAT_KEYWORDS);setInputValue('basicMembers',values.WECHAT_ALLOWED_MEMBERS);setInputValue('basicReplyCooldown',values.WECHAT_REPLY_COOLDOWN_SECONDS);setInputValue('basicMaxReplies',values.WECHAT_MAX_REPLIES_PER_HOUR);const setChecked=(id,value)=>{const el=$(id);if(el)el.checked=boolValue(value)};setInputValue('basicProactiveInterval',values.WECHAT_PROACTIVE_MIN_INTERVAL_SECONDS??300);setInputValue('basicProactiveMax',values.WECHAT_PROACTIVE_MAX_PER_HOUR??6);setInputValue('basicProactiveProbability',values.WECHAT_PROACTIVE_PROBABILITY??0.4);setChecked('basicProactive',values.WECHAT_PROACTIVE_REPLY);setChecked('basicPersonaFilter',values.PERSONA_STYLE_FILTER??true);setInputValue('basicPersonaMaxEmoji',values.PERSONA_MAX_EMOJI??1);setChecked('basicTrainingExport',values.TRAINING_EXPORT_ENABLED);setInputValue('basicTrainingExportHour',values.TRAINING_EXPORT_HOUR??23);setInputValue('basicTrainingExportMinute',values.TRAINING_EXPORT_MINUTE??30);document.querySelectorAll('[data-quick-env]').forEach(input=>{input.checked=boolValue(values[input.dataset.quickEnv]);input.disabled=false});renderAdvanced(data)}
    async function loadConfig(){try{state.config=await request('/api/config');renderConfig(state.config)}catch(e){toast(e.message,true)}}
    function renderAdvanced(data){const advancedFields=data.fields.filter(f=>!f.hidden&&!BASIC_ENVS.has(String(f.env)));const categories=[...new Set(advancedFields.map(f=>f.category))];const tabs=$('configTabs');const form=$('configForm');if(!categories.length){tabs.innerHTML='';form.innerHTML='<div class="empty">暂无更多设置</div>';return}let active=tabs.dataset.active||categories[0];if(!categories.includes(active))active=categories[0];tabs.dataset.active=active;tabs.innerHTML=categories.map(c=>'<button type="button" class="tab '+(c===active?'active':'')+'" data-category="'+esc(c)+'">'+esc(c)+'</button>').join('');tabs.querySelectorAll('[data-category]').forEach(button=>button.addEventListener('click',()=>selectConfigTab(button.dataset.category)));const values=data.values||{};const fields=advancedFields.filter(f=>f.category===active);form.innerHTML=fields.map(f=>{const env=String(f.env);const value=values[env]??'';const secret=f.secret?' <span>'+((data.configured_secrets||{})[env]?'已配置':'未配置')+'</span>':'';let input='';if(f.type==='checkbox')input='<input type="checkbox" data-env="'+esc(env)+'" '+(boolValue(value)?'checked':'')+'>';else if(f.type==='select')input='<select data-env="'+esc(env)+'">'+(f.options||[]).map(o=>'<option value="'+esc(o)+'" '+(String(value)===String(o)?'selected':'')+'>'+esc(o)+'</option>').join('')+'</select>';else{const type=f.type==='password'?'password':f.type==='number'?'number':'text';const placeholder=f.secret&&((data.configured_secrets||{})[env])?'已配置，留空保持原值':(f.description||'');input='<div class="secret-line"><input type="'+type+'" data-env="'+esc(env)+'" value="'+(f.secret?'':esc(value))+'" placeholder="'+esc(placeholder)+'">'+(f.secret?'<label class="clear-secret"><input type="checkbox" data-clear="'+esc(env)+'"> 清空</label>':'')+'</div>'}return '<div class="field '+(f.type==='text'&&f.description&&f.description.length>65?'full':'')+'"><label>'+esc(f.label)+secret+'</label>'+input+'<small>'+esc(f.description||'')+'</small></div>'}).join('')}
    function selectConfigTab(category){$('configTabs').dataset.active=category;renderAdvanced(state.config)}
    async function saveBasicConfig(){const values={WECHAT_GROUP_NAMES:$('basicGroups').value,WECHAT_GROUP_NAME:'',WECHAT_SELF_NAME:$('basicSelfName').value,WECHAT_AI_ALIASES:$('basicAliases').value,WECHAT_KEYWORDS:$('basicKeywords').value,WECHAT_ALLOWED_MEMBERS:$('basicMembers').value,WECHAT_REPLY_COOLDOWN_SECONDS:$('basicReplyCooldown').value,WECHAT_MAX_REPLIES_PER_HOUR:$('basicMaxReplies').value,WECHAT_PROACTIVE_REPLY:$('basicProactive').checked,WECHAT_PROACTIVE_MIN_INTERVAL_SECONDS:$('basicProactiveInterval').value,WECHAT_PROACTIVE_MAX_PER_HOUR:$('basicProactiveMax').value,WECHAT_PROACTIVE_PROBABILITY:$('basicProactiveProbability').value,PERSONA_STYLE_FILTER:$('basicPersonaFilter').checked,PERSONA_MAX_EMOJI:$('basicPersonaMaxEmoji').value,TRAINING_EXPORT_ENABLED:$('basicTrainingExport').checked,TRAINING_EXPORT_HOUR:$('basicTrainingExportHour').value,TRAINING_EXPORT_MINUTE:$('basicTrainingExportMinute').value};const button=document.querySelector('#config .settings-card .settings-actions .btn');button.disabled=true;try{await savePartial(values);$('basicSaveHint').textContent='已保存，助手已重新加载';toast('常用设置已保存')}catch(e){toast(e.message,true)}finally{button.disabled=false}}
    async function saveConfig(){try{const values={};document.querySelectorAll('#configForm [data-env]').forEach(input=>{values[input.dataset.env]=input.type==='checkbox'?input.checked:input.value});const clear=[...document.querySelectorAll('#configForm [data-clear]:checked')].map(input=>input.dataset.clear);const result=await savePartial(values,clear);toast(result.restart_required?'已保存。端口或绑定地址变更后请重启页面。':'更多设置已保存')}catch(e){toast(e.message,true)}}
    async function platformOpen(platform){try{await post('/api/platforms/open',{platform});toast('已请求打开 '+platform);await refresh(true)}catch(e){toast(e.message,true)}}
    async function platformBrowse(platform){try{const query=prompt('关键词（留空读取推荐内容）','')??'';state.lastItems=await post('/api/platforms/browse',{platform,query,limit:20});$('platformItems').textContent=JSON.stringify(state.lastItems,null,2);await refresh(true)}catch(e){toast(e.message,true)}}
    async function platformComments(){try{if(!state.lastItems.length)throw new Error('请先读取内容');const item=state.lastItems[0];const itemId=prompt('条目 ID',item.item_id||'');if(!itemId)return;state.lastComments=await post('/api/platform-comments',{platform:item.platform,item_id:itemId,limit:50});$('platformComments').textContent=JSON.stringify(state.lastComments,null,2)}catch(e){toast(e.message,true)}}
    async function platformPlan(type){try{const platform=prompt('平台：douyin 或 xiaohongshu',state.lastItems[0]?.platform||'douyin')||'douyin';const text=prompt(type==='comment'?'评论内容':'回复内容','');if(!text)return;const ids=(prompt('要 @ 的唯一用户 ID（逗号分隔；留空表示不 @）','')??'').split(',').map(v=>v.trim()).filter(Boolean);const body={platform,action_type:type,text,mention_ids:ids,dry_run:false};if(type==='comment')body.item_id=prompt('条目 ID',state.lastItems[0]?.item_id||'');else body.comment_id=prompt('评论 ID',state.lastComments[0]?.comment_id||'');const result=await post('/api/platform-actions/plan',body);toast('动作已入队：'+(result.action_key||''));await refresh(true)}catch(e){toast(e.message,true)}}
    async function platformExecute(){try{const key=prompt('粘贴待确认的 action_key');if(!key)return;await post('/api/platform-actions/execute',{action_key:key,confirm:true});toast('已提交平台动作');await refresh(true)}catch(e){toast(e.message,true)}}
    document.querySelectorAll('.nav a').forEach(link=>link.addEventListener('click',()=>{document.querySelectorAll('.nav a').forEach(item=>item.classList.remove('active'));link.classList.add('active')}));
    loadConfig();refresh(true);setInterval(()=>refresh(false),2000);
  </script>
</body>
</html>
"""
