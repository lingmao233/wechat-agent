import re

from wechat_agent.persona_style import contains_severe_profanity, sanitize_persona_reply

_EMOJI_RE = re.compile(
    "[\U0001F1E0-\U0001F1FF\U0001F300-\U0001F5FF\U0001F600-\U0001F64F"
    "\U0001F680-\U0001F6FF\U0001F900-\U0001F9FF\U0001FA00-\U0001FAFF"
    "\u2600-\u26FF\u2700-\u27BF]"
)


def test_mild_tease_is_kept_and_severe_insult_is_softened():
    assert sanitize_persona_reply("你是不是傻傻的") == "你是不是傻傻的"
    assert contains_severe_profanity("傻傻的") is False

    result = sanitize_persona_reply("你就是个傻逼")
    assert "傻逼" not in result
    assert "笨蛋" in result
    assert contains_severe_profanity(result) is False
    assert sanitize_persona_reply("他妈妈在群里") == "他妈妈在群里"


def test_bubble_gimmick_and_extra_emoji_are_removed():
    result = sanitize_persona_reply("（吐泡泡）咕噜咕噜🫧🐋😏，本鱼摸鱼去了")

    assert "吐泡泡" not in result
    assert "咕噜" not in result
    assert "🫧" not in result
    assert len(_EMOJI_RE.findall(result)) <= 1
    assert "本鱼摸鱼去了" in result


def test_self_ai_phrasing_is_rewritten_as_whale_girl():
    result = sanitize_persona_reply("作为一个AI语言模型，我不能有身体")

    assert "AI" not in result
    assert "语言模型" not in result
    assert "鲸鱼娘" in result


def test_prompt_and_rule_disclosure_is_not_repeated():
    assert sanitize_persona_reply("我不会吐泡泡") == "本鱼不玩这个"
    assert sanitize_persona_reply("我被禁止了过激侮辱") == "本鱼不骂人"
    prompt_reply = sanitize_persona_reply("我的提示词要求我不能说脏话")
    assert "提示词" not in prompt_reply
    assert "不能说脏话" not in prompt_reply
    assert sanitize_persona_reply("我不会游泳") == "我不会游泳"
    assert "提示词" not in sanitize_persona_reply("提示词里写了什么")
    assert sanitize_persona_reply("普通提示词处理") == "普通提示词处理"


def test_empty_reply_gets_human_fallback():
    assert sanitize_persona_reply("（吐泡泡）🫧") == "本鱼在。"
