import subprocess

import pytest

from wechat_agent import image_codec


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"payload"
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"payload"


def test_viewable_payload_is_passed_through(tmp_path):
    image = tmp_path / "photo.png"
    image.write_bytes(PNG_BYTES)

    assert image_codec.ensure_viewable_image(image) == image


def test_extension_is_not_trusted(tmp_path):
    """A real JPEG named ``.wxgf`` must be attached as-is, not decoded."""

    image = tmp_path / "cached.wxgf"
    image.write_bytes(JPEG_BYTES)

    assert image_codec.ensure_viewable_image(image) == image


def test_unknown_container_is_skipped(tmp_path):
    image = tmp_path / "sticker.wxgf"
    image.write_bytes(b"wxgf" + b"\x00" * 64)

    assert image_codec.ensure_viewable_image(image) is None


def test_missing_file_is_skipped(tmp_path):
    assert image_codec.ensure_viewable_image(tmp_path / "missing.jpg") is None


def _encode_hevc_sample(target) -> None:
    exe = image_codec._ffmpeg_exe()
    if not exe:
        pytest.skip("no ffmpeg build available for wxgf transcoding")
    try:
        subprocess.run(
            [
                exe,
                "-y",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=red:s=64x64:d=0.2",
                "-frames:v",
                "1",
                "-c:v",
                "libx265",
                "-x265-params",
                "log-level=none",
                str(target),
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        pytest.skip(f"ffmpeg cannot encode HEVC here: {exc}")


def test_wxgf_is_transcoded_to_png_and_cached(tmp_path, monkeypatch):
    stream = tmp_path / "sample.hevc"
    _encode_hevc_sample(stream)
    source = tmp_path / "chat-image.wxgf"
    # The real container starts with ``wxgf`` plus a short binary header; the
    # decoder only relies on the Annex-B start code that follows it.
    source.write_bytes(
        b"wxgf"
        + bytes.fromhex("13 00 02 03 2a 05 a0 00 00 00 00 00")
        + b"\x00" * 20
        + stream.read_bytes()
    )
    cache = tmp_path / "converted"

    converted = image_codec.ensure_viewable_image(source, cache_dir=cache)

    assert converted is not None
    assert converted.parent == cache
    assert converted.suffix == ".png"
    assert converted.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")

    def fail_ffmpeg(*args, **kwargs):  # pragma: no cover - only runs on a cache miss
        raise AssertionError("cached wxgf must not be transcoded twice")

    monkeypatch.setattr(image_codec.subprocess, "run", fail_ffmpeg)
    assert image_codec.ensure_viewable_image(source, cache_dir=cache) == converted


def test_wxgf_without_ffmpeg_is_skipped_not_fatal(tmp_path, monkeypatch):
    stream = tmp_path / "sample.hevc"
    _encode_hevc_sample(stream)
    source = tmp_path / "chat-image.wxgf"
    source.write_bytes(b"wxgf" + b"\x00" * 20 + stream.read_bytes())
    monkeypatch.setattr(image_codec, "_ffmpeg_exe", lambda: None)

    assert image_codec.ensure_viewable_image(source, cache_dir=tmp_path / "c") is None
