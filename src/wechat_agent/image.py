from __future__ import annotations

"""Image generation and local media preparation for the WeChat agent.

The text model and image model are separate clients. A generation response is
materialized to a bounded local file so the Weixin adapter can send a native
image attachment instead of pasting a URL as plain text. OpenAI-compatible
providers differ slightly in their image response shape, so both URL and
``b64_json`` rows are accepted.
"""

import base64
from dataclasses import dataclass, field
from datetime import timedelta
import logging
import mimetypes
from pathlib import Path
import re
import tempfile
import time
import unicodedata
from urllib.parse import quote, urlparse
from uuid import uuid4

import httpx

from .config import Settings


log = logging.getLogger(__name__)


class ImageModelError(RuntimeError):
    """A safe, user-facing image provider failure."""


@dataclass(slots=True)
class ImageReply:
    text: str
    model: str
    used_api: bool
    urls: list[str] = field(default_factory=list)
    # Generated files are sent by the Weixin adapter and cleaned by the agent
    # after the send attempt. They are never emitted as chat text.
    local_paths: list[str] = field(default_factory=list)


class ImageClient:
    """Detect natural image requests and call a second model endpoint."""

    # Broad enough for natural prompts such as “生成一张打麻将图片” and
    # “给我一张海边日落图”, while excluding common image-understanding or
    # forwarding requests.
    _IMAGE_INTENT = re.compile(
        r"(?:生图|文生图|出图|画图|绘图|image\s+generation|text[- ]to[- ]image)"
        r"|(?:生成|制作|绘制|画|设计|创作|做|来|给我|请)(?:\s*(?:一张|一幅|一个|一個|张|个))?"
        r".{0,100}(?:图片|图像|海报|插画|头像|壁纸|照片|表情包|图|image|picture|illustration)",
        re.IGNORECASE,
    )
    _IMAGE_NEGATIVE = re.compile(
        r"(?:识别|分析|描述|看看|查看|转发|发给我|发送|下载|保存|找一张|有没有)"
        r"|(?:发|转发)\s*(?:一张|一幅|一个|一個|张|个)\s*(?:图片|图像|照片|图|image|picture)",
        re.IGNORECASE,
    )
    _XML_START = re.compile(r"(?:<\?xml\b|<msg\b|<appmsg\b)", re.IGNORECASE)
    _WXID_PREFIX = re.compile(r"^\s*wxid_[^:\s]+\s*:\s*", re.IGNORECASE)

    def __init__(self, settings: Settings):
        self.settings = settings

    @classmethod
    def _clean_text(cls, text: str) -> str:
        value = unicodedata.normalize("NFKC", str(text or ""))
        value = "".join(char for char in value if unicodedata.category(char) not in {"Cf"})
        # A forwarded/link message is serialized as XML by Weixin. Its title
        # can contain “生图” even when the user only forwarded a reference.
        # Do not classify XML metadata as a fresh generation request.
        xml_match = cls._XML_START.search(value)
        if xml_match:
            value = value[: xml_match.start()]
        value = cls._WXID_PREFIX.sub("", value)
        return " ".join(value.split()).strip()

    def should_generate(self, text: str) -> bool:
        value = self._clean_text(text)
        if not value or self._IMAGE_NEGATIVE.search(value):
            return False
        return bool(self._IMAGE_INTENT.search(value))

    def prompt(self, text: str) -> str:
        """Return the visual brief without Weixin addressing metadata."""

        value = self._clean_text(text)
        aliases = sorted(
            (alias.strip() for alias in self.settings.wechat_ai_aliases if alias and alias.strip()),
            key=len,
            reverse=True,
        )
        for alias in aliases:
            value = re.sub(
                rf"@?\s*{re.escape(alias)}(?=\s|$|[,，:：])",
                " ",
                value,
                count=1,
                flags=re.IGNORECASE,
            )
        return " ".join(value.split()).strip(" ：:，,。.!！") or "请生成一张图片"

    def configured(self) -> bool:
        return bool(
            self.settings.image_model_enabled
            and self.settings.image_model_base_url.strip()
            and self.settings.image_model_api_key.strip()
            and self.settings.image_model.strip()
        )

    def upload_configured(self) -> bool:
        return bool(
            self.settings.image_upload_enabled
            and self.settings.oss_region.strip()
            and self.settings.oss_endpoint.strip()
            and self.settings.oss_bucket.strip()
            and self.settings.oss_access_key_id.strip()
            and self.settings.oss_access_key_secret.strip()
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
        }
        # GPT-image style gateways commonly return b64_json and reject the
        # older DALL-E-only response_format field. “auto”/empty omits it and
        # accepts either response shape.
        response_format = str(self.settings.image_model_response_format or "").strip().casefold()
        if response_format and response_format != "auto":
            payload["response_format"] = response_format

        url = self.settings.image_model_base_url.rstrip("/") + "/images/generations"
        headers = {
            "Authorization": f"Bearer {self.settings.image_model_api_key.strip()}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        timeout = self._http_timeout(self.settings.image_model_timeout_seconds)
        response: httpx.Response | None = None
        last_timeout: Exception | None = None
        for attempt in range(2):
            try:
                with httpx.Client(timeout=timeout, follow_redirects=True, trust_env=False) as client:
                    response = client.post(url, headers=headers, json=payload)
                break
            except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout) as exc:
                last_timeout = exc
                if attempt == 0:
                    time.sleep(0.8)
                    continue
                raise ImageModelError("生图模型连接或读取超时，请检查网关连通性") from exc
            except httpx.HTTPError as exc:
                raise ImageModelError(f"生图模型网络错误：{type(exc).__name__}") from exc
        if response is None:
            raise ImageModelError("生图模型请求未完成") from last_timeout

        if response.status_code >= 400:
            detail = self._response_detail(response)
            log.warning(
                "image model rejected request status=%s model=%s detail=%s",
                response.status_code,
                self.settings.image_model,
                detail,
            )
            raise ImageModelError(f"生图模型返回 HTTP {response.status_code}：{detail}")
        try:
            data = response.json()
        except ValueError as exc:
            raise ImageModelError("生图模型返回了无法解析的 JSON") from exc

        rows = data.get("data", []) if isinstance(data, dict) else []
        if not isinstance(rows, list) or not rows:
            raise ImageModelError("生图模型返回为空")

        local_paths: list[str] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            encoded = row.get("b64_json")
            if encoded:
                local_paths.append(self._save_base64(str(encoded)))
                continue
            remote_url = row.get("url")
            if remote_url:
                local_paths.append(self._download_to_local(str(remote_url)))
        if not local_paths:
            raise ImageModelError("生图模型没有返回可用图片")

        urls: list[str] = []
        if self.settings.image_upload_enabled and self.upload_configured():
            for path in local_paths:
                try:
                    urls.append(self._upload_generated_image(path))
                except Exception as exc:  # noqa: BLE001 - local attachment remains usable
                    # OSS is an optional distribution enhancement.  A
                    # provider-generated local file can still be sent through
                    # Weixin, so storage outages must not turn a successful
                    # generation into a failed reply.
                    log.warning(
                        "image upload failed; continuing with local attachment "
                        "path=%s error=%s",
                        path,
                        type(exc).__name__,
                    )
        elif self.settings.image_upload_enabled:
            log.warning("image upload enabled but OSS is incomplete; sending local attachment")

        url_text = "\n".join(urls)

        return ImageReply(
            text=(
                f"已生成图片（模型：{self.settings.image_model}）"
                + (f"\n{url_text}" if url_text else "")
            ),
            model=self.settings.image_model,
            used_api=True,
            urls=urls,
            local_paths=local_paths,
        )

    @staticmethod
    def _http_timeout(seconds: float) -> httpx.Timeout:
        total = max(1.0, float(seconds))
        return httpx.Timeout(
            timeout=total,
            connect=min(8.0, total),
            write=min(30.0, total),
            pool=min(10.0, total),
        )

    @staticmethod
    def _response_detail(response: httpx.Response) -> str:
        try:
            body = response.json()
            detail = body.get("error") if isinstance(body, dict) else body
            if isinstance(detail, dict):
                detail = detail.get("message") or detail.get("detail") or detail
            value = str(detail)
        except ValueError:
            value = response.text
        value = re.sub(r"(?i)bearer\s+\S+", "Bearer <redacted>", value)
        value = " ".join(value.split())
        return value[:400] or "provider did not include details"

    def _output_dir(self) -> Path:
        return Path(self.settings.image_output_dir)

    def _save_base64(self, encoded: str) -> str:
        output_dir = self._output_dir()
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / f"{uuid4().hex}.png"
        try:
            content = base64.b64decode(encoded, validate=False)
        except (ValueError, TypeError) as exc:
            raise ImageModelError("生图模型返回的 base64 图片无效") from exc
        if len(content) > self.settings.image_upload_max_bytes:
            raise ImageModelError("生成图片超过本地大小上限")
        path.write_bytes(content)
        return str(path.resolve())

    def _download_to_local(self, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ImageModelError("生图模型返回的图片 URL 无效")
        output_dir = self._output_dir()
        output_dir.mkdir(parents=True, exist_ok=True)
        timeout = self._http_timeout(self.settings.image_upload_timeout_seconds)
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True, trust_env=False) as client:
                with client.stream("GET", value) as response:
                    response.raise_for_status()
                    mime_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > self.settings.image_upload_max_bytes:
                            raise ImageModelError("生成图片超过本地大小上限")
                        chunks.append(chunk)
            content = b"".join(chunks)
        except ImageModelError:
            raise
        except (httpx.HTTPError, OSError) as exc:
            raise ImageModelError(f"生成图片下载失败：{type(exc).__name__}") from exc
        suffix = mimetypes.guess_extension(mime_type or "image/png") or ".png"
        path = output_dir / f"{uuid4().hex}{suffix}"
        path.write_bytes(content)
        return str(path.resolve())

    def _upload_generated_image(self, value: str) -> str:
        content, mime_type, filename = self._read_image_value(value)
        if len(content) > self.settings.image_upload_max_bytes:
            raise ImageModelError("生成图片超过 OSS 大小上限")
        object_key = self._oss_object_key(mime_type)
        suffix = Path(filename).suffix or mimetypes.guess_extension(mime_type) or ".png"
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix="wechat-agent-image-",
                suffix=suffix,
                delete=False,
            ) as temporary:
                temporary.write(content)
                temporary_path = Path(temporary.name)
            client, oss = self._oss_client()
            client.put_object_from_file(
                oss.PutObjectRequest(
                    bucket=self.settings.oss_bucket.strip(),
                    key=object_key,
                    content_type=mime_type,
                    content_disposition="inline",
                ),
                str(temporary_path),
            )
            return self._oss_url(client, oss, object_key)
        except ImportError as exc:
            raise ImageModelError("OSS SDK 未安装") from exc
        except Exception as exc:  # noqa: BLE001 - SDK exception hierarchy is not stable
            raise ImageModelError(f"OSS 上传失败：{type(exc).__name__}") from exc
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def _read_image_value(self, value: str) -> tuple[bytes, str, str]:
        if value.startswith("data:"):
            header, encoded = value.split(",", 1)
            mime_type = header[5:].split(";", 1)[0] or "image/png"
            content = base64.b64decode(encoded, validate=False)
            return content, mime_type, self._filename_for_mime(mime_type)

        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"}:
            path = Path(value)
            if path.is_file():
                content = path.read_bytes()
                mime_type = mimetypes.guess_type(path.name)[0] or "image/png"
                return content, mime_type, path.name

        timeout = self._http_timeout(self.settings.image_upload_timeout_seconds)
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True, trust_env=False) as client:
                with client.stream("GET", value) as response:
                    response.raise_for_status()
                    mime_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > self.settings.image_upload_max_bytes:
                            raise ImageModelError("generated_image_exceeds_upload_limit")
                        chunks.append(chunk)
                    content = b"".join(chunks)
        except ImageModelError:
            raise
        except httpx.HTTPError as exc:
            raise ImageModelError(f"图片下载失败：{type(exc).__name__}") from exc
        filename = Path(parsed.path).name or self._filename_for_mime(mime_type or "image/png")
        return content, mime_type or "image/png", filename

    def cleanup_local(self, paths: list[str]) -> None:
        root = (Path("state") / "generated").resolve()
        for value in paths:
            try:
                path = Path(value).resolve()
                if path.is_file() and root in path.parents:
                    path.unlink(missing_ok=True)
            except OSError:
                log.debug("unable to clean generated image path", exc_info=True)

    def _oss_client(self):
        try:
            import alibabacloud_oss_v2 as oss
        except ImportError as exc:  # pragma: no cover - deployment packaging guard
            raise ImportError("alibabacloud-oss-v2 is required for OSS uploads") from exc
        credentials = oss.credentials.StaticCredentialsProvider(
            self.settings.oss_access_key_id.strip(),
            self.settings.oss_access_key_secret.strip(),
        )
        config = oss.Config(
            region=self.settings.oss_region.strip(),
            endpoint=self.settings.oss_endpoint.strip(),
            credentials_provider=credentials,
        )
        return oss.Client(config), oss

    def _oss_object_key(self, mime_type: str) -> str:
        prefix = self.settings.oss_prefix.strip().strip("/")
        extension = mimetypes.guess_extension(mime_type) or ".png"
        filename = f"{uuid4().hex}{extension}"
        parts = [part for part in (prefix, "wechat-agent", "generated", filename) if part]
        return "/".join(parts)

    def _oss_url(self, client, oss, object_key: str) -> str:
        public_base = self.settings.oss_public_base_url.strip().rstrip("/")
        if public_base:
            parsed = urlparse(public_base)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ImageModelError("OSS_PUBLIC_BASE_URL 必须是 HTTP(S) 地址")
            return f"{public_base}/{quote(object_key, safe='/')}"
        signed = client.presign(
            oss.GetObjectRequest(
                bucket=self.settings.oss_bucket.strip(),
                key=object_key,
            ),
            expires=timedelta(seconds=int(self.settings.oss_signed_url_ttl_seconds)),
        )
        url = getattr(signed, "url", "")
        if not isinstance(url, str) or not url.strip():
            raise ImageModelError("OSS 没有返回可访问地址")
        return url.strip()

    @staticmethod
    def _filename_for_mime(mime_type: str) -> str:
        extension = mimetypes.guess_extension(mime_type) or ".png"
        return f"generated-{uuid4().hex}{extension}"
