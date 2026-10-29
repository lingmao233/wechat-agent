from wechat_agent.prompt_profiles import build_profile_summary


def test_summary_keeps_useful_signal_and_drops_audit_fields():
    user = {
        "member_name": "小明",
        "preferences": ["喜欢深色界面"],
        "interests": ["部署与运维"],
        "behavior": {
            "message_count": 42,
            "average_message_length": 12.0,
            "expression_habits": ["short_turns"],
            "common_topics": ["部署与运维"],
            "marker_counts": {"哈哈": 3},
            "evidence_message_ids": ["1", "2"],
        },
    }
    ai = {
        "message_count": 42,
        "reply_length": "short",
        "formality": "casual",
        "humor": "occasional",
        "common_vocabulary": ["笑死"],
        "synthesized": {
            "jokes": ["鹅鸭杀"],
            "bit_routines": ["先装傻再反转"],
            "tease_patterns": ["你是不是傻"],
            "comfort_patterns": ["抱抱，别想太多"],
            "meme_triggers": ["下雨→带伞梗"],
            "message_shape": "短句连发，少用句号",
        },
        "history": [{"previous": "x" * 500}],
        "evidence_message_ids": ["1"],
    }
    task = {"goal": "完成接口", "next_action": "补测试", "history": [{"x": 1}]}

    summary = build_profile_summary(user, ai, task, max_chars=4000)

    assert "喜欢深色界面" in summary
    assert "回复形态：short" in summary
    assert "1~2 句" in summary
    assert "笑死" in summary
    assert "整活套路：先装傻再反转" in summary
    assert "互损/钓鱼：你是不是傻" in summary
    assert "安慰话术：抱抱，别想太多" in summary
    assert "接梗触发：下雨→带伞梗" in summary
    assert "发消息习惯：短句连发，少用句号" in summary
    assert "完成接口" in summary
    assert "补测试" in summary
    assert "evidence" not in summary
    assert "previous" not in summary
    assert "message_count" not in summary
    assert len(summary) <= 4000


def test_summary_handles_empty_profiles_and_hard_budget():
    assert build_profile_summary({}, {}, {}) == ""

    summary = build_profile_summary(
        {"preferences": ["很长的偏好" * 20]},
        {"common_vocabulary": ["词" * 40] * 20},
        {},
        max_chars=200,
    )
    assert len(summary) <= 220
    assert summary.endswith("（画像已截断）") or len(summary) <= 200
