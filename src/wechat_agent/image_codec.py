"""Turn cached WeChat media into a payload a vision model can actually read.

Weixin 4.x stores chat pictures in a proprietary ``.wxgf`` container whose
payload is a raw HEVC (H.265) Annex-B stream.  Posting those bytes to an
OpenAI-compatible vision endpoint as ``data:image/jpeg`` makes the provider
reject the *entire* request with ``HTTP 400`` (``unsupported image``), which
used to drop the text answer as well.

The helpers here decode that container and re-encode the first frame as PNG,
which every vision endpoint accepts.  Results are cached next to the media
directory so a picture that stays inside the prompt context is transcoded
only once.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from hashlib import sha1
from pathlib import Path

log = logging.getLogger(__name__)

#: Formats the OpenAI-compatible vision APIs accept, keyed by file signature.
VIEWABLE_FORMATS: tuple[tuple[bytes, str, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    (b"GIF87a", "image/gif", ".gif"),
    (b"GIF89a", "image/gif", ".gif"),
)

_WXGF_MAGIC = b"wxgf"
_ANNEX_B_START = b"\x00\x00\x00\x01"
_MAX_SOURCE_BYTES = 40 * 1024 * 1024
_CONVERT_TIMEOUT_SECONDS = 30


def sniff_viewable(data: bytes) -> tuple[str, str] | None:
    """Return ``(extension, mime)`` when ``data`` already is a vision image."""

    for prefix, mime, suffix in VIEWABLE_FORMATS:
        if data.startswith(prefix):
            return suffix, mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp", "image/webp"
    return None


def is_viewable(path: str | Path) -> bool:
    """Best-effort check used to decide whether a file needs transcoding."""

    try:
        with Path(path).open("rb") as handle:
            return sniff_viewable(handle.read(16)) is not None
    except OSError:
        return False


def _ffmpeg_exe() -> str | None:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # pragma: no cover - optional dependency
        return None


def _hevc_payload(data: bytes) -> bytes | None:
    """Extract the HEVC Annex-B stream that follows the ``wxgf`` header."""

    start = data.find(_ANNEX_B_START)
    if start < 0 or start + len(_ANNEX_B_START) >= len(data):
        return None
    return data[start:]


def _cache_path(source: Path, cache_dir: Path | None) -> Path:
    try:
        stat = source.stat()
        fingerprint = f"{stat.st_size}:{stat.st_mtime_ns}"
    except OSError:
        fingerprint = "0:0"
    digest = sha1(f"{source.name}:{fingerprint}".encode("utf-8")).hexdigest()[:12]
    target_dir = cache_dir if cache_dir is not None else source.parent / "converted"
    return Path(target_dir) / f"{source.stem}-{digest}.png"


def convert_wxgf(source: str | Path, cache_dir: str | Path | None = None) -> Path | None:
    """Decode one ``wxgf`` file to PNG, returning the cached path.

    ``None`` means the file is not a decodable ``wxgf`` container, or no usable
    ffmpeg build is available.  Callers should simply omit the image and keep
    the text answer instead of failing the whole completion.
    """

    path = Path(source)
    cache_root = Path(cache_dir) if cache_dir is not None else None
    target = _cache_path(path, cache_root)
    if target.is_file() and is_viewable(target):
        return target

    try:
        if path.stat().st_size > _MAX_SOURCE_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if data[:4] != _WXGF_MAGIC and _ANNEX_B_START not in data[:128]:
        return None
    payload = _hevc_payload(data)
    if payload is None:
        log.debug("wxgf container has no HEVC payload image=%s", path.name)
        return None

    exe = _ffmpeg_exe()
    if not exe:
        log.warning("cannot decode wxgf image=%s; no ffmpeg available", path.name)
        return None

    with tempfile.TemporaryDirectory() as workdir:
        raw = Path(workdir) / "frame.hevc"
        rendered = Path(workdir) / "frame.png"
        raw.write_bytes(payload)
        command = [
            exe,
            "-y",
            "-v",
            "error",
            "-f",
            "hevc",
            "-i",
            str(raw),
            "-frames:v",
            "1",
            "-f",
            "image2",
            str(rendered),
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                timeout=_CONVERT_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("ffmpeg wxgf decode failed image=%s error=%s", path.name, exc)
            return None
        if result.returncode != 0 or not rendered.is_file():
            detail = (result.stderr or b"").decode("utf-8", "replace").strip()
            log.warning(
                "ffmpeg wxgf decode failed image=%s rc=%s detail=%s",
                path.name,
                result.returncode,
                detail[:200],
            )
            return None
        rendered_bytes = rendered.read_bytes()
    if sniff_viewable(rendered_bytes) is None:
        log.warning("wxgf transcode produced no readable image image=%s", path.name)
        return None

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=str(target.parent), suffix=".tmp", delete=False
        ) as handle:
            handle.write(rendered_bytes)
            temporary = Path(handle.name)
        os.replace(temporary, target)
    except OSError as exc:
        log.warning("cannot cache decoded wxgf image=%s error=%s", path.name, exc)
        return None
    log.info(
        "wxgf image decoded image=%s bytes=%s cache=%s",
        path.name,
        len(rendered_bytes),
        target.name,
    )
    return target


def ensure_viewable_image(
    source: str | Path, cache_dir: str | Path | None = None
) -> Path | None:
    """Return a vision-compatible path for ``source``, transcoding if needed.

    Extensions are never trusted: WeChat may hand over a ``.wxgf`` that already
    contains JPEG bytes, and the reverse happens too.  Everything is decided by
    the file signature.
    """

    path = Path(source)
    try:
        if not path.is_file():
            return None
        with path.open("rb") as handle:
            header = handle.read(128)
    except OSError:
        return None
    if sniff_viewable(header) is not None:
        return path
    if header[:4] == _WXGF_MAGIC or _ANNEX_B_START in header:
        return convert_wxgf(path, cache_dir=cache_dir)
    return None
