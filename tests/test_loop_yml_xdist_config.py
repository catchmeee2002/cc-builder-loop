"""B1: 仓库根 .claude/loop.yml 的 test stage 改为 pytest-xdist -n 16 并行、超时 600s，
其余字段（proof_runner / max_iterations / evidence_neutral_paths）保持不变。
"""

from __future__ import annotations

from pathlib import Path

from builder_loop.config import load_loop_config

ROOT = Path(__file__).resolve().parents[1]


def test_b1_test_stage_uses_xdist_and_600s_timeout():
    cfg = load_loop_config(ROOT)
    assert len(cfg.pass_cmd) == 1
    stage = cfg.pass_cmd[0]
    assert stage.stage == "test"
    assert stage.cmd == "python3 -m pytest -p no:html -p no:cacheprovider -q -n 16 tests"
    assert stage.timeout == 600


def test_b1_invariant_proof_runner_unchanged():
    cfg = load_loop_config(ROOT)
    assert cfg.proof_runner == {"framework": "pytest", "cmd": "python3 -m pytest -p no:html"}


def test_b1_invariant_max_iterations_unchanged():
    cfg = load_loop_config(ROOT)
    assert cfg.max_iterations == 5


def test_b1_invariant_evidence_neutral_paths_unchanged():
    cfg = load_loop_config(ROOT)
    assert sorted(cfg.evidence_neutral_paths) == sorted(["docs/**", "CHANGELOG.md", "README.md"])
    assert len(cfg.evidence_neutral_paths) == 3
