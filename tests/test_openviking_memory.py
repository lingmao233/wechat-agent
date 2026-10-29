import json

import httpx

from wechat_agent.config import Settings
from wechat_agent.openviking_memory import OpenVikingMemoryClient, peer_id_for_scope, scope_uri


def test_scope_uri_contains_stable_ids_and_sanitizes_segments():
    uri = scope_uri(
        namespace="test namespace",
        account_id="account/1",
        group_id="group A",
        member_id="member X",
        task_id="task-1",
    )
    assert uri == (
        "viking://user/test_namespace/peers/user-test_namespace-peers-wechatagent/"
        "memories/profiles/accounts/account_1/groups/group_A/members/member_X/tasks/task-1"
    )


def test_peer_id_is_deterministic_and_separates_scope_dimensions():
    member_in_a = peer_id_for_scope(
        account_id="account-1",
        group_id="group-a",
        member_id="member-x",
    )
    member_in_b = peer_id_for_scope(
        account_id="account-1",
        group_id="group-b",
        member_id="member-x",
    )
    other_member = peer_id_for_scope(
        account_id="account-1",
        group_id="group-a",
        member_id="member-y",
    )
    assert member_in_a == peer_id_for_scope(
        account_id="account-1",
        group_id="group-a",
        member_id="member-x",
    )
    assert member_in_a != member_in_b
    assert member_in_a != other_member
    assert "/" not in member_in_a and "\\" not in member_in_a
    assert member_in_a.startswith("wechat-agent-member-")

    group_peer = peer_id_for_scope(account_id="account-1", group_id="group-a")
    task_peer = peer_id_for_scope(
        account_id="account-1",
        group_id="group-a",
        task_id="task-1",
    )
    member_task_peer = peer_id_for_scope(
        account_id="account-1",
        group_id="group-a",
        member_id="member-x",
        task_id="task-1",
    )
    assert group_peer != task_peer
    assert task_peer != member_task_peer


def test_openviking_write_and_search_keep_scope_isolation(monkeypatch):
    settings = Settings(
        openviking_enabled=True,
        openviking_url="https://example.test",
        openviking_api_key="test-key",
        openviking_namespace="test-run",
    )
    client = OpenVikingMemoryClient(settings)
    calls = []

    def fake_request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path.endswith("/search/find"):
            return {
                "memories": [
                    {
                        "score": 0.95,
                        "uri": "viking://user/test-run/memories/a",
                        "content": json.dumps(
                            {
                                "memory_id": "a",
                                "value": "喜欢深色界面",
                                "scope": {
                                    "account_id": "account-1",
                                    "group_id": "group-a",
                                    "member_id": "member-x",
                                },
                                "source_message_ids": ["msg-1"],
                            },
                            ensure_ascii=False,
                        ),
                    },
                    {
                        "score": 0.99,
                        "uri": "viking://user/test-run/memories/b",
                        "content": {
                            "memory_id": "b",
                            "value": "不应泄漏",
                            "scope": {
                                "account_id": "account-1",
                                "group_id": "group-b",
                                "member_id": "member-x",
                            },
                        },
                    },
                ]
            }
        return {}

    monkeypatch.setattr(client, "_request", fake_request)
    assert client.put_memory(
        {
            "memory_id": "a",
            "memory_type": "preference",
            "scope": {
                "account_id": "account-1",
                "group_id": "group-a",
                "member_id": "member-x",
            },
            "value": "喜欢深色界面",
            "source_message_ids": ["msg-1"],
        }
    ) is True
    hits = client.search(
        "界面偏好",
        account_id="account-1",
        group_id="group-a",
        member_id="member-x",
    )

    assert [hit.memory_id for hit in hits] == ["a"]
    assert calls[0][0] == "POST"
    assert calls[0][1] == "/api/v1/content/write"
    assert "group-a" in calls[0][2]["json"]["content"]
    assert "/memories/facts/accounts/account-1/groups/group-a/members/member-x/preference/" in calls[0][2]["json"]["uri"]
    search_call = next(call for call in calls if call[1].endswith("/search/find"))
    assert search_call[2]["json"]["target_uri"].endswith("/peers/user-test-run-peers-wechatagent/memories")
    assert not any(path.endswith("/commit") for _, path, _ in calls)


def test_search_does_not_fall_back_to_unscoped_memories(monkeypatch):
    settings = Settings(
        openviking_enabled=True,
        openviking_url="https://example.test",
        openviking_api_key="test-key",
        openviking_namespace="test-run",
    )
    client = OpenVikingMemoryClient(settings)
    targets = []

    def fake_request(method, path, **kwargs):
        if path.endswith("/search/find"):
            target = kwargs["json"]["target_uri"]
            targets.append(target)
            return {"memories": []}
        return {}

    monkeypatch.setattr(client, "_request", fake_request)
    hits = client.search(
        "不存在的旧数据",
        account_id="account-1",
        group_id="group-a",
        member_id="member-x",
    )

    assert hits == []
    assert targets[0].endswith("/peers/user-test-run-peers-wechatagent/memories")
    assert len(targets) == 1


def test_openviking_connection_failure_is_cooled_down(monkeypatch):
    settings = Settings(
        openviking_enabled=True,
        openviking_url="https://example.test",
        openviking_api_key="test-key",
        openviking_connect_timeout_seconds=2,
        openviking_failure_cooldown_seconds=300,
    )
    client = OpenVikingMemoryClient(settings)
    calls = 0

    class Client:
        def __init__(self, **kwargs):
            nonlocal calls
            calls += 1

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def request(self, *args, **kwargs):
            raise httpx.ConnectTimeout("simulated connection timeout")

    monkeypatch.setattr("wechat_agent.openviking_memory.httpx.Client", Client)

    assert client.health() is False
    assert client.health() is False
    assert calls == 1


def test_raw_messages_are_written_to_group_and_member_tree(monkeypatch):
    settings = Settings(
        openviking_enabled=True,
        openviking_url="https://example.test",
        openviking_api_key="test-key",
        openviking_namespace="test-run",
    )
    client = OpenVikingMemoryClient(settings)
    calls = []

    def fake_request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {}

    monkeypatch.setattr(client, "_request", fake_request)
    assert client.put_raw_message(
        account_id="account-1",
        group_id="group-a",
        member_id="member-x",
        member_name="小明",
        content="普通聊天行为",
        message_id="msg-1",
    ) is True
    uris = [call[2]["json"]["uri"] for call in calls]
    assert len(uris) == 2
    assert any("/memories/messages/accounts/account-1/groups/group-a/" in uri for uri in uris)
    assert any("/memories/messages/accounts/account-1/groups/group-a/members/member-x/" in uri for uri in uris)
    payloads = [json.loads(call[2]["json"]["content"]) for call in calls]
    assert all(item["memory_type"] == "raw_message" for item in payloads)
    assert all(item["source_message_ids"] == ["msg-1"] for item in payloads)

