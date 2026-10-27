from __future__ import annotations

"""Optional image-generation model for the WeChat agent.

The text model and image model are intentionally separate clients.  The
adapter speaks the widely used OpenAI-compatible ``/v1/images/generations``
contract and returns URLs (or a bounded local file path for ``b64_json``
responses) as text because the current Weixin UI adapter only sends text.
"""

import base64
from dataclasses import dataclass, field
from pathlib import Path
import re
from uuid import uuid4

import httpx

from .config import Settings


@dataclass(slots=True)
class ImageReply:
    text: str
    model: str
    used_api: bool
    urls: list[str] = field(default_factory=list)


class ImageClient:
    """Route explicit image requests to a second model endpoint."""

    _IMAGE_HINTS = re.compile(
        r"生成(?:一张|张)?(?:图片|图像|海报|插画|头像)|画一张|帮我画|生图|文生图|绘图|出图|generate\s+(?:an?\s+)?image|draw\s+(?:an?\s+)?(?:image|picture)",
        re.IGNORECASE,
    )

    def __init__(self, settings: Settings):
        self.settings = settings

    def should_generate(self, text: str) -> bool:
        return bool(self._IMAGE_HINTS.search(text or ""))

    def prompt(self, text: str) -> str:
        """Remove only the addressing prefix; keep the user's visual brief."""
        value = " ".join((text or "").split()).strip()
        aliases = sorted(
            (alias.strip() for alias in self.settings.wechat_ai_aliases if alias and alias.strip()),
            key=len,
            reverse=True,
        )
        for alias in aliases:
            value = re.sub(rf"@?\s*{re.escape(alias)}", " ", value, flags=re.IGNORECASE)
        return " ".join(value.split()).strip(" ：:，,。.!！") or "请生成一张图片"

    def configured(self) -> bool:
        return bool(
            self.settings.image_model_enabled
            and self.settings.image_model_base_url.strip()
            and self.settings.image_model_api_key.strip()
            and self.settings.image_model.strip()
        )

    def generate(self, prompt: str) -> ImageReply:
        if not self.configured():
            return ImageReply(
                text=(
                    "生图模型尚未配置。请设置 IMAGE_MODEL_ENABLED=true、"
                    "IMAGE_MODEL_BASE_URL、IMAGE_MODEL_API_KEY 和 IMAGE_MODEL。"
                ),
                model="unconfigured",
                used_api=False,
            )

        payload: dict[str, object] = {
            "model": self.settings.image_model,
            "prompt": prompt,
            "n": 1,
            "size": self.settings.image_model_size,
            "response_format": self.settings.image_model_response_format,
        }
        url = self.settings.image_model_base_url.rstrip("/") + "/images/generations"
        headers = {
            "Authorization": f"Bearer {self.settings.image_model_api_key.strip()}",
            "Accept": "application/json",
        }
        with httpx.Client(timeout=self.settings.image_model_timeout_seconds) as client:
            response = client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()

        rows = data.get("data", []) if isinstance(data, dict) else []
        if not isinstance(rows, list) or not rows:
            raise ValueError("image_model_returned_no_data")
        urls: list[str] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            remote_url = row.get("url")
            if remote_url:
                urls.append(str(remote_url))
                continue
            encoded = row.get("b64_json")
            if encoded:
                urls.append(self._save_base64(str(encoded)))
        if not urls:
            raise ValueError("image_model_returned_no_url")
        return ImageReply(
            text=(f"已生成图片（模型：{self.settings.image_model}）：\n" + "\n".join(urls)),
            model=self.settings.image_model,
            used_api=True,
            urls=urls,
        )

    @staticmethod
    def _save_base64(encoded: str) -> str:
        output_dir = Path("state/generated")
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / f"{uuid4().hex}.png"
        path.write_bytes(base64.b64decode(encoded))
        return str(path.resolve())
