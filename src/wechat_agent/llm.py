from __future__ import annotations

import json
from dataclasses import dataclass

import httpx

from .config import Settings
from .models import MemoryFact


@dataclass(slots=True)
class LLMReply:
    text: str
    model: str
    used_api: bool


class LLMClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    def reply(
        self,
        incoming: str,
        member_name: str,
        group_name: str,
        memories: list[MemoryFact],
    ) -> LLMReply:
        if not self.settings.llm_api_key:
            return LLMReply(
                text=f"[AI测试] {member_name}，我收到了：{incoming[:120]}",
                model="stub",
                used_api=False,
            )

        memory_text = "\n".join(
            f"- {fact.content}（{fact.status}，置信度 {fact.confidence:.2f}）" for fact in memories
        ) or "（暂无可用长期记忆）"
        system = (
            "你是测试群中的 AI，必须明确自己是 AI。只使用当前群和当前群友的上下文。"
            "推断字段只能作为假设，不能说成已确认事实。不要泄露其他群的内容。"
        )
        user = (
            f"群：{group_name}\n群友：{member_name}\n\n"
            f"可用记忆：\n{memory_text}\n\n消息：{incoming}"
        )
        payload = {
            "model": self.settings.llm_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.7,
            "max_tokens": self.settings.llm_max_output_tokens,
        }
        url = self.settings.llm_base_url.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {self.settings.llm_api_key}"}
        with httpx.Client(timeout=self.settings.llm_timeout_seconds) as client:
            response = client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
        text = data["choices"][0]["message"]["content"].strip()
        return LLMReply(text=text, model=self.settings.llm_model, used_api=True)
