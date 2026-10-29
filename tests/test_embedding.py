import pytest

from wechat_agent.config import Settings
from wechat_agent.embedding import EmbeddingClient, EmbeddingError


class FakeResponse:
    def __init__(self, body, status_code=200):
        self.body = body
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"status:{self.status_code}")

    def json(self):
        return self.body


class FakeClient:
    responses = []
    calls = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, *, headers, json):
        self.calls.append({"url": url, "headers": headers, "json": json})
        return self.responses.pop(0)


def test_multimodal_ark_contract_is_one_text_vector_per_request(monkeypatch):
    FakeClient.calls = []
    FakeClient.responses = [
        FakeResponse({"data": {"embedding": [0.1, 0.2, 0.3]}}),
        FakeResponse({"data": {"embedding": [0.4, 0.5, 0.6]}}),
    ]
    monkeypatch.setattr("wechat_agent.embedding.httpx.Client", FakeClient)
    settings = Settings(
        embed_base="https://example.test/api/v3/embeddings/multimodal",
        embed_model="doubao-embedding-vision-251215",
        embed_key="test-key",
        embedding_enabled=True,
        embed_dimension=0,
    )

    result = EmbeddingClient(settings).embed(["偏好深色", "设计界面"], is_query=True)

    assert result.dimension == 3
    assert len(result.vectors) == 2
    assert [call["url"] for call in FakeClient.calls] == [settings.embed_base] * 2
    assert FakeClient.calls[0]["json"]["input"] == [{"type": "text", "text": "偏好深色"}]
    assert "dimensions" not in FakeClient.calls[0]["json"]


def test_standard_openai_contract_keeps_batch_shape_and_strict_dimension(monkeypatch):
    FakeClient.calls = []
    FakeClient.responses = [
        FakeResponse(
            {
                "data": [
                    {"embedding": [0.1, 0.2, 0.3], "index": 0},
                    {"embedding": [0.4, 0.5, 0.6], "index": 1},
                ]
            }
        )
    ]
    monkeypatch.setattr("wechat_agent.embedding.httpx.Client", FakeClient)
    settings = Settings(
        embed_base="https://example.test/v1",
        embed_model="text-embedding-test",
        embed_key="test-key",
        embedding_enabled=True,
        embed_dimension=3,
    )

    result = EmbeddingClient(settings).embed(["a", "b"])

    assert result.dimension == 3
    assert FakeClient.calls[0]["url"] == "https://example.test/v1/embeddings"
    assert FakeClient.calls[0]["json"]["dimensions"] == 3


def test_dimension_mismatch_is_reported_without_provider_body(monkeypatch):
    FakeClient.calls = []
    FakeClient.responses = [FakeResponse({"data": {"embedding": [0.1, 0.2, 0.3]}})]
    monkeypatch.setattr("wechat_agent.embedding.httpx.Client", FakeClient)
    settings = Settings(
        embed_base="https://example.test/api/v3/embeddings/multimodal",
        embed_model="doubao-embedding-vision-251215",
        embed_key="test-key",
        embedding_enabled=True,
        embed_dimension=1024,
    )

    with pytest.raises(EmbeddingError, match="embedding_dimension_mismatch:3:1024"):
        EmbeddingClient(settings).embed_one("测试")
