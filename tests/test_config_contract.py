import copy
import json

import pytest

from builder_loop import contract as C
from builder_loop.config import load_loop_config, parse_yaml_subset
from builder_loop.errors import Problem
from conftest import CONTRACT, PROOF_RUNNER_CMD, contract_with, write_plan


def test_yaml_subset_parses_block_and_flow():
    text = """
pass_cmd:
  - stage: lint
    cmd: "ruff check ."   # comment
    timeout: 10
  - { stage: test, cmd: 'pytest -q', timeout: 30 }
max_iterations: 5
layout:
  source_dirs: ["src"]
worktree:
  root: ../wt
"""
    d = parse_yaml_subset(text)
    assert d["pass_cmd"][0] == {"stage": "lint", "cmd": "ruff check .", "timeout": 10}
    assert d["pass_cmd"][1] == {"stage": "test", "cmd": "pytest -q", "timeout": 30}
    assert d["max_iterations"] == 5 and d["layout"]["source_dirs"] == ["src"] and d["worktree"]["root"] == "../wt"


def test_load_loop_config_and_proof_runner(repo):
    cfg = load_loop_config(repo.root)
    assert cfg.pass_cmd[0].stage == "test" and cfg.max_iterations == 3
    assert cfg.proof_runner == {"framework": "pytest", "cmd": PROOF_RUNNER_CMD}
    yml = repo.root / ".claude" / "loop.yml"
    base = "pass_cmd:\n  - stage: t\n    cmd: \"true\"\n"
    yml.write_text(base)
    assert load_loop_config(repo.root).proof_runner == {"framework": "pytest", "cmd": "python3 -m pytest"}
    yml.write_text(base + "proof_runner:\n  framework: generic\n")
    with pytest.raises(Problem) as ei:
        load_loop_config(repo.root)
    assert ei.value.code == "CONFIG_PROOF_RUNNER_INVALID"
    yml.write_text(base + "proof_runner:\n  framework: junit5\n  cmd: x\n")
    with pytest.raises(Problem):
        load_loop_config(repo.root)
    yml.write_text("pass_cmd: []\n")
    with pytest.raises(Problem) as ei:
        load_loop_config(repo.root)
    assert ei.value.code == "CONFIG_PASS_CMD_EMPTY" and ei.value.exit_code == 2
    yml.unlink()
    with pytest.raises(Problem) as ei:
        load_loop_config(repo.root)
    assert ei.value.code == "CONFIG_MISSING"


def test_contract_parse_and_digest_stable():
    text = "x\n<!-- builder-loop-contract -->\n```json\n" + json.dumps(CONTRACT) + "\n```\n<!-- /builder-loop-contract -->\n"
    c1, c2 = C.parse_contract_text(text), C.parse_contract_text(text)
    assert C.facet_digests(c1) == C.facet_digests(c2)
    assert c1["assurance"]["proof_kinds"] == list(C.PROOF_KINDS)
    assert c1["mission"]["behaviors"][0]["boundaries"] == ["a or b is 0"]


@pytest.mark.parametrize("mutate", [
    lambda c: c["mission"].pop("behaviors"),
    lambda c: c["mission"]["behaviors"].append({"id": "B1", "given": "", "when": "", "then": ""}),
    lambda c: c["mission"]["behaviors"][0].update(boundaries="not a list"),
    lambda c: c["mission"]["behaviors"][0].update(proof="weakest"),
    lambda c: c["mission"].update(mock_strategy=["x"]),
    lambda c: c["assurance"].update(required=["tester"]),
    lambda c: c["assurance"].update(required=["machine", "proof"]),
    lambda c: c["authority"].update(tester_write=[]),
    lambda c: c.update(schema="nope"),
])
def test_contract_validation_rejects(mutate):
    c = copy.deepcopy(CONTRACT)
    mutate(c)
    with pytest.raises(Problem) as ei:
        C.validate_contract(c)
    assert ei.value.code == "CONTRACT_INVALID"


def test_duplicate_tag_rejected():
    body = "<!-- builder-loop-contract -->\n{}\n<!-- /builder-loop-contract -->\n"
    with pytest.raises(Problem) as ei:
        C.parse_contract_text(body + body)
    assert ei.value.code == "CONTRACT_DUPLICATE"


def test_path_matching_and_cover():
    assert C.path_matches("src/**", "src/a/b.py") and C.path_matches("src/**", "src/x.py")
    assert not C.path_matches("src/*", "src/a/b.py")
    assert C.path_matches("**/conftest.py", "tests/unit/conftest.py") and C.path_matches("**/conftest.py", "conftest.py")
    assert C.path_matches("docs/", "docs/a.md")
    assert C.glob_covers(["src/**"], "src/sub/**") and C.glob_covers(["src/**"], "src/x.py")
    assert not C.glob_covers(["src/**"], "lib/**") and not C.glob_covers(["src/*.py"], "src/**")


def test_write_rejection_single_entry():
    """tester_write 优先 + 控制面规则：checkpoint / PreToolUse / mutation patch 共用这一个判定。"""
    auth = C.freeze_authority({"authority": {"builder_write": ["**"], "tester_write": ["tests/**"], "protected_paths": ["secret.txt"]}})["authority"]
    assert C.write_rejection(auth, "builder", "src/a.py") is None
    assert C.write_rejection(auth, "builder", "tests/test_a.py") == "tester_owned"  # 两边 glob 都命中 → 归 tester
    assert C.write_rejection(auth, "tester", "tests/test_a.py") is None
    assert C.write_rejection(auth, "tester", "src/a.py") == "builder_owned"
    assert C.write_rejection(auth, "builder", "secret.txt") == "protected"
    # 控制面：builder 靠 glob 顺带命中 → 拒；新建的子目录 conftest 一视同仁；tester_write 内的归 tester
    assert C.write_rejection(auth, "builder", "pyproject.toml") == "control_file"
    assert C.write_rejection(auth, "builder", "pkg/sub/conftest.py") == "control_file"
    assert C.write_rejection(auth, "tester", "tests/conftest.py") is None
    # 字面点名 = 计划明确授权
    named = C.freeze_authority({"authority": {"builder_write": ["src/**", "pyproject.toml"], "tester_write": ["tests/**"], "protected_paths": []}})["authority"]
    assert C.write_rejection(named, "builder", "pyproject.toml") is None
    assert C.write_rejection(named, "builder", "docs/x.md") == "outside_authority"


def test_classify_change():
    old = copy.deepcopy(CONTRACT)
    old["assurance"]["machine_commands"] = [{"stage": "test", "cmd": "pytest", "timeout": 60}]
    old["assurance"]["proof_runner"] = {"framework": "pytest", "cmd": "python3 -m pytest"}
    new = copy.deepcopy(old)
    assert C.classify_change(old, new) == [C.CHANGE_NEUTRAL]
    new["mission"]["objective"] = "changed"
    assert C.CHANGE_MISSION in C.classify_change(old, new)
    new = copy.deepcopy(old); new["authority"]["builder_write"].append("lib/**")
    assert C.classify_change(old, new) == [C.CHANGE_AUTHORITY_EXPAND]
    new = copy.deepcopy(old); new["authority"]["builder_write"] = ["src/sub/**"]
    assert C.classify_change(old, new) == [C.CHANGE_NEUTRAL]
    new = copy.deepcopy(old); new["authority"]["protected_paths"] = []
    assert C.classify_change(old, new) == [C.CHANGE_AUTHORITY_EXPAND]  # 保护集缩小 = 写权限扩大
    new = copy.deepcopy(old); new["assurance"]["required"] = ["machine", "reviewer"]
    assert C.classify_change(old, new) == [C.CHANGE_ASSURANCE_DOWNGRADE]
    new = copy.deepcopy(old); new["assurance"]["machine_commands"][0]["cmd"] = "true"
    assert C.classify_change(old, new) == [C.CHANGE_ASSURANCE_DOWNGRADE]
    new = copy.deepcopy(old); new["assurance"]["proof_runner"]["cmd"] = "echo ok"
    assert C.classify_change(old, new) == [C.CHANGE_ASSURANCE_DOWNGRADE]
    new = copy.deepcopy(old); new["mission"]["behaviors"][0]["proof"] = "reviewed-boundaries"
    assert set(C.classify_change(old, new)) == {C.CHANGE_MISSION, C.CHANGE_ASSURANCE_DOWNGRADE}


def test_contract_validate_check_repo(repo, cli):
    (repo.root / "pyproject.toml").write_text("[project]\nname='x'\n")
    repo.commit_all()
    write_plan(repo.root, contract_with(**{"authority.builder_write": ["**"]}), "wide.md")
    out = cli("contract", "validate", "--plan", str(repo.root / "wide.md"), "--check-repo")
    assert "pyproject.toml" in out["repo"]["control_files_protected"]
    assert "tests/test_foo.py" in out["repo"]["overlap_resolved_to_tester"]
    assert out["repo"]["proof_runner"]["cmd"] == PROOF_RUNNER_CMD
    write_plan(repo.root, contract_with(**{"authority.target_branch": "nope"}), "bad.md")
    err = cli("contract", "validate", "--plan", str(repo.root / "bad.md"), "--check-repo", expect=1)
    assert err["code"] == "CONTRACT_REPO_CHECK_FAILED"
    assert cli("contract", "validate", "--plan", str(repo.root / "bad.md"))["valid"]  # 不带 --check-repo 只校结构
