"""B2: proof pass 之后，某条声明 test_id 在反例下从未变红（per_id 为 green，或 missing）时，
`bl brief --role reviewer` 的文本输出要点名这些 test_id 并给出字面提醒句，引导 reviewer 把它们当
搭便车嫌疑逐条判断。

用 L.mutate 直接在已经真实跑通的 proof evidence 上覆写 per_id 字段来构造各种取值组合：这是本 run 的
Mock 策略里"端到端: conftest 的临时 git 仓 + drive_to_proof_pass 之类的流程"指定的路数——先用
drive_to_proof_pass 拿到一份真实、结构合法的 proof evidence，再只改 per_id 这一个字段来模拟
discrimination() 已经产出结果之后的样子（这个字段今天还不存在，属于本 run 要交付的新行为）。
"""

from __future__ import annotations

from builder_loop import ledger as L

from conftest import drive_to_proof_pass

SENTENCE = ("下面这些 test_id 在反例下从未变红：它们没有被证明有鉴别力，可能是断言的条件在本设计下恒真。"
            "逐条判断它是真的在约束 behavior，还是搭了同组其它测试的便车。")


def _set_per_id(ledger_path, per_id: dict) -> None:
    with L.mutate(ledger_path) as lg:
        lg["evidence"]["proof"]["details"]["groups"][0]["counterexample"]["per_id"] = per_id


def _drop_per_id(ledger_path) -> None:
    with L.mutate(ledger_path) as lg:
        lg["evidence"]["proof"]["details"]["groups"][0]["counterexample"].pop("per_id", None)


def test_brief_flags_never_red_test_ids(started, cli, hook):
    drive_to_proof_pass(started, cli, hook)
    _set_per_id(started["ledger"], {"tests/test_mul.py::test_mul": "green"})
    text = cli("brief", "--session", "S1", "--role", "reviewer")
    assert isinstance(text, str)
    assert "tests/test_mul.py::test_mul" in text
    assert SENTENCE in text


def test_brief_flags_missing_test_ids_too(started, cli, hook):
    drive_to_proof_pass(started, cli, hook)
    _set_per_id(started["ledger"], {"tests/test_mul.py::test_mul": "missing"})
    text = cli("brief", "--session", "S1", "--role", "reviewer")
    assert "tests/test_mul.py::test_mul" in text
    assert SENTENCE in text


def test_brief_silent_when_all_ids_are_red(started, cli, hook):
    drive_to_proof_pass(started, cli, hook)
    _set_per_id(started["ledger"], {"tests/test_mul.py::test_mul": "red"})
    text = cli("brief", "--session", "S1", "--role", "reviewer")
    assert SENTENCE not in text


def test_brief_silent_without_proof_evidence(started, cli, hook):
    # 还没跑过 proof：reviewer brief 照常给出审查待办，不提这句话
    text = cli("brief", "--session", "S1", "--role", "reviewer")
    assert SENTENCE not in text
    assert "[review]" in text or "review" in text


def test_brief_silent_and_no_crash_on_old_ledger_without_per_id(started, cli, hook):
    drive_to_proof_pass(started, cli, hook)
    _drop_per_id(started["ledger"])
    text = cli("brief", "--session", "S1", "--role", "reviewer")
    assert SENTENCE not in text


def test_tester_brief_unaffected_by_discrimination_feature(started, cli, hook):
    """不变量：tester 的 brief 不因这条能力变化。"""
    drive_to_proof_pass(started, cli, hook)
    _set_per_id(started["ledger"], {"tests/test_mul.py::test_mul": "green"})
    text = cli("brief", "--session", "S1", "--role", "tester")
    assert SENTENCE not in text
