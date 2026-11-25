"""integration-dispatch 的会话整理、规划模型与派活完成判据文本契约。"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "integration-dispatch" / "SKILL.md"


def skill_text() -> str:
    return SKILL.read_text(encoding="utf-8")


def test_every_new_assignment_requires_session_reset_or_compaction() -> None:
    text = skill_text()
    assert "每次派新活前都必须执行 `/new` 或 `/compact`" in text
    assert "有未决交付或占用不明时不得复用" in text
    assert "不按闲置时长判断" in text


def test_session_and_planning_model_are_verified_before_delivery() -> None:
    text = skill_text()
    section = text.split("## 准备会话", 1)[1].split("## 交付任务背景", 1)[0]
    compact = section.index("每次派新活前都必须执行")
    model = section.index("设置并回读用户当场指定的高档规划模型")
    ready = section.index("用 `ListAgents` 确认整理后的会话空闲")
    deliver = section.index("再用 `SendMessage` 派活")
    assert compact < model < ready < deliver
    assert "另读状态栏确认规划模型" in section
    assert "Sonnet" not in text
    assert "deepseek" not in text


def test_dispatch_completion_requires_plain_introduction_and_waiting() -> None:
    text = skill_text()
    assert "两三句人话" in text
    assert "已收到开发的两三句人话介绍" in text
    assert "仍停在 plan、run 和代码改动之前" in text
    assert "已告诉用户去哪个会话亲自确认" in text
    assert "用户确认开工后，本 skill 结束" in text
    assert "执行阶段的模型选择也不在本 skill 处理" in text
