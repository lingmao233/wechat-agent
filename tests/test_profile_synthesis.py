from wechat_agent.profile_synthesis import build_transcript, synthesize_group_profile


class FakeLLM:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def complete(self, system, user, **kwargs):
        self.calls.append((system, user, kwargs))
        return self.reply


def test_synthesis_parses_json_and_drops_unknown_fields():
    llm = FakeLLM(
        "```json\n"
        '{"culture":"爱开玩笑，节奏快","reply_shape":"short",'
        '"reply_examples":["笑死，确实","别急，我看看"],'
        '"nicknames":{"Xi":"西西"},"jokes":["鹅鸭杀"],'
        '"bit_routines":["先装傻再反转"],"tease_patterns":["你是不是傻"],'
        '"comfort_patterns":["抱抱，别想太多"],"meme_triggers":["下雨→带伞梗"],'
        '"message_shape":"短句连发，少用句号",'
        '"taboos":["政治"],"rules":["先接梗再回答"],'
        '"member_notes":{"Xi":"爱提问"},"extra":"drop"}'
        "\n```"
    )
    data = synthesize_group_profile(llm, [("Xi", "这题怎么做？"), ("瑾年", "哈哈哈")])

    assert data["culture"] == "爱开玩笑，节奏快"
    assert data["reply_shape"] == "short"
    assert data["reply_examples"] == ["笑死，确实", "别急，我看看"]
    assert data["nicknames"] == {"Xi": "西西"}
    assert data["bit_routines"] == ["先装傻再反转"]
    assert data["tease_patterns"] == ["你是不是傻"]
    assert data["comfort_patterns"] == ["抱抱，别想太多"]
    assert data["meme_triggers"] == ["下雨→带伞梗"]
    assert data["message_shape"] == "短句连发，少用句号"
    assert data["member_notes"] == {"Xi": "爱提问"}
    assert "extra" not in data
    assert len(llm.calls) == 1
    assert "这题怎么做" in llm.calls[0][1]


def test_transcript_keeps_newest_lines_within_budget():
    items = [(f"m{index}", "内容" * 20) for index in range(100)]

    transcript = build_transcript(items, max_chars=300, per_message_chars=40)

    assert "m99" in transcript
    assert "m0：" not in transcript
    assert len(transcript) <= 300


def test_empty_transcript_does_not_call_model():
    llm = FakeLLM("{}")
    assert synthesize_group_profile(llm, []) == {}
    assert llm.calls == []


def test_invalid_reply_shape_and_non_list_fields_are_ignored():
    llm = FakeLLM('{"reply_shape":"paragraph","jokes":"not-a-list","culture":"  "}')
    assert synthesize_group_profile(llm, [("Xi", "你好")]) == {}
