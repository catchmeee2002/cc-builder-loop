import copy
import json

import pytest

from builder_loop import contract as C
from builder_loop.config import load_loop_config, parse_yaml_subset
from builder_loop.errors import Problem
from conftest import CONTRACT


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


def test_load_loop_config_errors(repo):
    cfg = load_loop_config(repo.root)
    assert cfg.pass_cmd[0].stage == "test" and cfg.max_iterations == 3
    (repo.root / ".claude" / "loop.yml").write_text("pass_cmd: []\n")
    with pytest.raises(Problem) as ei:
        load_loop_config(repo.root)
    assert ei.value.code == "CONFIG_PASS_CMD_EMPTY" and ei.value.exit_code == 2
    (repo.root / ".claude" / "loop.yml").unlink()
    with pytest.raises(Problem) as ei:
        load_loop_config(repo.root)
    assert ei.value.code == "CONFIG_MISSING"


def test_contract_parse_and_digest_stable():
    text = "x\n<!-- builder-loop-contract -->\n```json\n" + json.dumps(CONTRACT) + "\n```\n<!-- /builder-loop-contract -->\n"
    c1 = C.parse_contract_text(text)
    c2 = C.parse_contract_text(text)
    assert C.facet_digests(c1) == C.facet_digests(c2)
    assert c1["assurance"]["proof_kinds"] == list(C.PROOF_KINDS)


@pytest.mark.parametrize("mutate,code", [
    (lambda c: c["mission"].pop("behaviors"), "CONTRACT_INVALID"),
    (lambda c: c["mission"]["behaviors"].append({"id": "B1", "given": "", "when": "", "then": ""}), "CONTRACT_INVALID"),
    (lambda c: c["assurance"].update(required=["tester"]), "CONTRACT_INVALID"),
    (lambda c: c["assurance"].update(required=["machine", "proof"]), "CONTRACT_INVALID"),
    (lambda c: c["authority"].update(tester_write=[]), "CONTRACT_INVALID"),
    (lambda c: c.update(schema="nope"), "CONTRACT_INVALID"),
])
def test_contract_validation_rejects(mutate, code):
    c = copy.deepcopy(CONTRACT)
    mutate(c)
    with pytest.raises(Problem) as ei:
        C.validate_contract(c)
    assert ei.value.code == code


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


def test_classify_change():
    old = copy.deepcopy(CONTRACT)
    old["assurance"]["machine_commands"] = [{"stage": "test", "cmd": "pytest", "timeout": 60}]
    new = copy.deepcopy(old)
    assert C.classify_change(old, new) == [C.CHANGE_NEUTRAL]
    new["mission"]["objective"] = "changed"
    assert C.CHANGE_MISSION in C.classify_change(old, new)
    new = copy.deepcopy(old); new["authority"]["builder_write"].append("lib/**")
    assert C.classify_change(old, new) == [C.CHANGE_AUTHORITY_EXPAND]
    new = copy.deepcopy(old); new["authority"]["builder_write"] = ["src/sub/**"]
    assert C.classify_change(old, new) == [C.CHANGE_NEUTRAL]
    new = copy.deepcopy(old); new["assurance"]["required"] = ["machine", "reviewer"]
    assert C.classify_change(old, new) == [C.CHANGE_ASSURANCE_DOWNGRADE]
    new = copy.deepcopy(old); new["assurance"]["machine_commands"][0]["cmd"] = "true"
    assert C.classify_change(old, new) == [C.CHANGE_ASSURANCE_DOWNGRADE]
