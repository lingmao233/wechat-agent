from wechat_agent.textnorm import normalize_chat_text


def test_strips_known_wxid_prefix_and_keeps_body():
    result = normalize_chat_text("wxid_abc123:\n我要退群了", sender_id="wxid_abc123")

    assert result.text == "我要退群了"
    assert result.visible_text == "我要退群了"
    assert result.prefix_sender == "wxid_abc123"
    assert result.kind == "text"


def test_preserves_legitimate_label_prefix():
    result = normalize_chat_text("注意：明天停电", sender_name="烛火")

    assert result.text == "注意：明天停电"
    assert result.prefix_sender == ""


def test_strips_known_display_name_prefix_only():
    result = normalize_chat_text("烛火：我要退群了", sender_name="烛火")

    assert result.text == "我要退群了"
    assert result.prefix_sender == "烛火"


def test_emoji_xml_becomes_placeholder_without_inflating_length():
    raw = 'wxid_abc:\n<msg><emoji md5="x" cdnurl="http://example/x" /></msg>'
    result = normalize_chat_text(raw, sender_id="wxid_abc")

    assert result.text == "[表情]"
    assert result.visible_text == ""
    assert result.sticker_count == 1
    assert result.kind == "emoji"
    assert len(result.text) < 10


def test_appmsg_title_stays_visible_for_topics():
    raw = "<msg><appmsg appid=''><title>今天彩票开奖了</title><type>5</type></appmsg></msg>"
    result = normalize_chat_text(raw)

    assert result.text == "[链接:今天彩票开奖了]"
    assert result.visible_text == "今天彩票开奖了"


def test_refermsg_becomes_quote_and_keeps_quoted_text():
    raw = (
        "<msg><appmsg><title>回复</title><type>57</type>"
        "<refermsg><displayname>甲</displayname><content>你昨天说的那个</content></refermsg>"
        "</appmsg></msg>"
    )
    result = normalize_chat_text(raw)

    assert result.text == "回复 [引用:你昨天说的那个]"
    assert result.quoted_text == "你昨天说的那个"
    assert result.visible_text == "回复"


def test_quoted_reply_keeps_the_typed_mention():
    raw = (
        '<?xml version="1.0"?><msg><appmsg appid="" sdkver="0">'
        "<title>@Away\u2005</title><type>57</type>"
        "<refermsg><type>1</type><content>凌歌骂你是废物 你忘记了吗</content></refermsg>"
        "</appmsg></msg>"
    )
    result = normalize_chat_text(raw, ai_aliases=["AI", "Away"])

    assert result.kind == "quote"
    assert result.addressed_as == "Away"
    assert result.text == "@AI [引用:凌歌骂你是废物 你忘记了吗]"
    assert result.quoted_text == "凌歌骂你是废物 你忘记了吗"


def test_ai_alias_is_normalized_and_recorded():
    result = normalize_chat_text("@Away 给我唱首歌", ai_aliases=["Away"])

    assert result.text == "@AI 给我唱首歌"
    assert result.addressed_as == "Away"
    assert normalize_chat_text("@所有人 开会", ai_aliases=["Away"]).text == "@所有人 开会"


def test_time_separator_and_system_rows_are_flagged():
    assert normalize_chat_text("16:18").is_system
    assert normalize_chat_text("16:18").text == ""
    assert normalize_chat_text("查看更多消息").is_system
    assert normalize_chat_text("你撤回了一条消息").is_system


def test_normalization_is_idempotent():
    first = normalize_chat_text("wxid_abc:\n@Away 你好", sender_id="wxid_abc", ai_aliases=["Away"])
    second = normalize_chat_text(first.text, ai_aliases=["Away"])

    assert first.text == "@AI 你好"
    assert second.text == first.text
    assert second.addressed_as == ""


def test_message_with_less_than_sign_is_not_treated_as_xml():
    result = normalize_chat_text("3<4 才对")

    assert result.kind == "text"
    assert result.text == "3<4 才对"
