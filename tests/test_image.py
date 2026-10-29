import base64
from pathlib import Path

from wechat_agent.config import Settings
from wechat_agent.image import ImageClient


def test_natural_image_intent_and_negative_cases():
    client = ImageClient(Settings(wechat_ai_aliases=["AI"]))

    assert client.should_generate("@AI 生成一张打麻将图片")
    assert client.should_generate("给我一张海边日落图")
    assert client.should_generate("画一个头像")
    assert not client.should_generate("帮我识别这张图片")
    assert not client.should_generate("请分析图片内容")
    assert not client.should_generate("给我发一张图片")
    assert not client.should_generate(
        '<msg><appmsg><title>生图：海边日落</title></appmsg></msg>'
    )


def test_auto_response_format_accepts_b64_and_oss_failure_keeps_local_file(
    monkeypatch, tmp_path
):
    captured = {}

    class Response:
        status_code = 200

        def json(self):
            return {"data": [{"b64_json": base64.b64encode(b"fake-png").decode()}]}

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, *, headers, json):
            captured["json"] = json
            return Response()

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("wechat_agent.image.httpx.Client", Client)
    settings = Settings(
        image_model_enabled=True,
        image_model_base_url="https://images.example.test/v1",
        image_model_api_key="test-key",
        image_model="gpt-image-test",
        image_model_response_format="auto",
        image_upload_enabled=True,
        oss_region="cn-shenzhen",
        oss_endpoint="https://oss.example.test",
        oss_bucket="bucket",
        oss_access_key_id="id",
        oss_access_key_secret="secret",
    )
    client = ImageClient(settings)
    monkeypatch.setattr(
        client,
        "_upload_generated_image",
        lambda path: (_ for _ in ()).throw(RuntimeError("oss down")),
    )

    reply = client.generate("海边日落")

    assert "response_format" not in captured["json"]
    assert reply.used_api is True
    assert len(reply.local_paths) == 1
    assert reply.urls == []
    assert reply.local_paths[0].endswith(".png")
    assert open(reply.local_paths[0], "rb").read() == b"fake-png"
    client.cleanup_local(reply.local_paths)


def test_generated_image_uses_configured_output_dir(tmp_path):
    target = tmp_path / "data-disk" / "generated"
    settings = Settings(image_output_dir=target)
    client = ImageClient(settings)

    saved = Path(client._save_base64(base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 8).decode()))

    assert saved.parent == target
    assert saved.is_file()

