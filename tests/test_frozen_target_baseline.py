"""run 起点冻结的事实：目标提交里的 loop.yml、上游同步状态、依赖目录来源。

B1 / B2 / ... / B8 的判据见本 run 的 contract；本文件是它们的证明名单来源。
起点（冻结基线）代码不做任何一项：start 读的是工作区 loop.yml、不查上游、不建依赖链接，
所以下面每条正向断言在起点上都会在 call 阶段失败（baseline-red）。

不变的现役行为（untracked loop.yml / ahead / 上游未前进 / 既有 TARGET_DRIFT / 未声明 link_paths）
作为 guard 留在同一文件，不列入 proof_spec。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

from builder_loop import ledger as L
from builder_loop import worktree as W

from conftest import (
    PROOF_RUNNER_CMD,
    PYTEST_CMD,
    RUNTIME,
    contract_with,
    drive_to_proof_pass,
    git,
    implement_mul,
    make_tester_result,
    mutation_patch,
    reviewer_pass,
    role_turn,
    write_mul_test,
    write_plan,
)

PASS_CMD = "python3 -c 'import sys; sys.exit(0)'"
MARKER = "dependency-marker-7b1e"


# ---------------------------------------------------------------- 构造仓与 loop.yml


def _loop_text(*, cmd: str = PASS_CMD, ready_cmd: str | None = None,
               link_paths: list[str] | None = None) -> str:
    lines = [
        "pass_cmd:",
        "  - stage: test",
        f'    cmd: "{cmd}"',
        "    timeout: 60",
    ]
    if ready_cmd is not None:
        lines.append(f'    ready_cmd: "{ready_cmd}"')
    lines += ["max_iterations: 3", "proof_runner:", "  framework: pytest", f'  cmd: "{PROOF_RUNNER_CMD}"']
    if link_paths is not None:
        lines.append("worktree:")
        lines.append("  link_paths:")
        lines.extend(f'    - "{p}"' for p in link_paths)
    return "\n".join(lines) + "\n"


def _write_loop(repo, text: str) -> None:
    (repo.root / ".claude" / "loop.yml").write_text(text, encoding="utf-8")


def _write_ignore(repo, *extra: str) -> None:
    (repo.root / ".gitignore").write_text(
        "".join(p + "\n" for p in (".claude/builder-loop/", "__pycache__/", *extra)), encoding="utf-8")


def _started(out: dict) -> dict:
    return {
        "repo": None,  # drive_to_proof_pass 只用 worktree / tester_worktree
        "run_id": out["run_id"],
        "ledger": Path(out["ledger"]),
        "worktree": Path(out["worktree"]),
        "tester_worktree": Path(out["tester_worktree"]),
        "target_start_head": out["target_start_head"],
    }


def _side_effects(repo) -> dict:
    """start 失败后不应留下的东西：ledger、worktree、分支。"""
    runs = repo.root / ".claude" / "builder-loop" / "runs"
    ledgers = sorted(str(p.relative_to(repo.root)) for p in runs.rglob("*")) if runs.exists() else []
    return {
        "ledgers": ledgers,
        "worktrees": git(repo.root, "worktree", "list", "--porcelain"),
        "branches": git(repo.root, "branch", "--list", "--format=%(refname:short)"),
    }


# ---------------------------------------------------------------- 本地 bare 仓充当 origin


def _setup_origin(repo, tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(origin)], check=True, capture_output=True)
    git(repo.root, "remote", "add", "origin", str(origin))
    git(repo.root, "push", "-q", "-u", "origin", "main")
    return origin


def _advance_origin(repo, tmp_path: Path, origin: Path, name: str = "upstream.txt") -> str:
    """在 origin 上多一个提交，fetch 回本地；本地 refs/heads/<target> 不动。"""
    seed = tmp_path / "seed"
    if not seed.exists():
        subprocess.run(["git", "clone", "-q", str(origin), str(seed)], check=True, capture_output=True)
        git(seed, "config", "user.email", "t@t")
        git(seed, "config", "user.name", "t")
    (seed / name).write_text("upstream\n", encoding="utf-8")
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", f"chore(upstream): [cr_id_skip] {name}")
    git(seed, "push", "-q", "origin", "main")
    git(repo.root, "fetch", "-q", "origin")
    return git(repo.root, "rev-parse", "origin/main")


def _head(repo, ref: str = "main") -> str:
    return git(repo.root, "rev-parse", ref)


def _ledger(out: dict) -> dict:
    return L.load(Path(out["ledger"]))


# ---------------------------------------------------------------- B1：loop.yml 来自目标提交


def test_b1_start_freezes_loop_config_from_target_commit(repo, cli):
    """目标提交里带 ready_cmd，工作区同名文件把它删掉且未提交：冻结的仍是提交里的那份。"""
    _write_loop(repo, _loop_text(cmd=PYTEST_CMD, ready_cmd="test -x marker"))
    sha = repo.commit_all("chore(fixture): [cr_id_skip] Ready probe loop config")
    # 工作区副本不含 ready_cmd，且不提交
    _write_loop(repo, _loop_text(cmd=PYTEST_CMD))

    out = cli("start", "--plan", str(repo.root / "plan.md"), "--session", "S1")

    lg = _ledger(out)
    assert lg["contract"]["assurance"]["machine_commands"][0]["ready_cmd"] == "test -x marker", lg["contract"]["assurance"]
    assert out["loop_config_source"]["from"] == f"{sha[:12]}:.claude/loop.yml", out.get("loop_config_source")
    assert out["loop_config_source"]["worktree_differs"] is True, out["loop_config_source"]


def test_b1_untracked_loop_yml_falls_back_to_worktree(repo, cli):
    """边界：目标提交未跟踪 .claude/loop.yml → 回退工作区，from == "worktree"。"""
    git(repo.root, "rm", "--cached", "-q", ".claude/loop.yml")
    _write_ignore(repo, ".claude/loop.yml")
    repo.commit_all("chore(fixture): [cr_id_skip] Untrack loop.yml")
    _write_loop(repo, _loop_text(cmd=PYTEST_CMD, ready_cmd="test -x marker"))

    out = cli("start", "--plan", str(repo.root / "plan.md"), "--session", "S1")

    assert out["loop_config_source"]["from"] == "worktree", out.get("loop_config_source")
    lg = _ledger(out)
    assert lg["contract"]["assurance"]["machine_commands"][0]["ready_cmd"] == "test -x marker", lg["contract"]["assurance"]


def test_b1_target_branch_commit_wins_over_current_checkout(repo, cli):
    """边界：--target dev 而当前 checkout main → 冻结 dev 提交里的那份 loop.yml。"""
    git(repo.root, "checkout", "-q", "-b", "dev")
    _write_loop(repo, _loop_text(cmd=PYTEST_CMD, ready_cmd="test -x devmarker"))
    dev_sha = repo.commit_all("chore(fixture): [cr_id_skip] Dev loop config")
    git(repo.root, "checkout", "-q", "main")
    assert git(repo.root, "rev-parse", "--abbrev-ref", "HEAD") == "main"

    out = cli("start", "--plan", str(repo.root / "plan.md"), "--session", "S1", "--target", "dev")

    lg = _ledger(out)
    assert out["target_branch"] == "dev"
    assert lg["contract"]["assurance"]["machine_commands"][0]["ready_cmd"] == "test -x devmarker", lg["contract"]["assurance"]
    assert out["loop_config_source"]["from"] == f"{dev_sha[:12]}:.claude/loop.yml", out.get("loop_config_source")


# ---------------------------------------------------------------- B2：behind


def test_b2_behind_blocks_start_with_error_and_no_side_effects(repo, cli, tmp_path):
    """本地与 origin/main 同源，远端多一个已 fetch 的提交 → exit 2 + 不产生任何现场。"""
    origin = _setup_origin(repo, tmp_path)
    remote_head = _advance_origin(repo, tmp_path, origin)
    local_head = _head(repo)
    assert local_head != remote_head
    before = _side_effects(repo)

    out = cli("start", "--plan", str(repo.root / "plan.md"), "--session", "S1", expect=2)

    assert out["code"] == "TARGET_NOT_IN_SYNC", out
    assert out["details"]["relation"] == "behind", out["details"]
    assert out["details"]["local_head"] == local_head, out["details"]
    assert out["details"]["remote_head"] == remote_head, out["details"]
    assert len(local_head) == len(remote_head) == 40
    assert _side_effects(repo) == before


def test_b2_allow_stale_target_acknowledges_and_records_event(repo, cli, tmp_path):
    """边界：--allow-stale-target 承认落后 → 启动成功，输出 acknowledged，ledger 有对应事件。"""
    origin = _setup_origin(repo, tmp_path)
    _advance_origin(repo, tmp_path, origin)

    out = cli("start", "--plan", str(repo.root / "plan.md"), "--session", "S1", "--allow-stale-target")

    assert out["target_baseline"]["acknowledged"] is True, out.get("target_baseline")
    lg = _ledger(out)
    events = lg.get("events") or []
    # 「对应 event」：承认落后必须留下一条可审计的事件（不冻结具体 kind 名）
    assert any("acknowledg" in json.dumps(e, ensure_ascii=False).lower() for e in events), events


def test_b2_in_sync_start_reports_in_sync(repo, cli, tmp_path):
    """不变量：本地与上游一致时 start 成功，relation == "in_sync"。"""
    _setup_origin(repo, tmp_path)

    out = cli("start", "--plan", str(repo.root / "plan.md"), "--session", "S1")

    assert out["target_baseline"]["relation"] == "in_sync", out.get("target_baseline")


# ---------------------------------------------------------------- B3：diverged


def test_b3_diverged_blocks_start(repo, cli, tmp_path):
    """本地有 origin 没有的提交，origin 也有本地没有的提交（已 fetch）→ exit 2。"""
    origin = _setup_origin(repo, tmp_path)
    (repo.root / "local-only.txt").write_text("local\n", encoding="utf-8")
    repo.commit_all("chore(local): [cr_id_skip] Local only")
    _advance_origin(repo, tmp_path, origin)
    assert _head(repo) != _head(repo, "origin/main")
    before = _side_effects(repo)

    out = cli("start", "--plan", str(repo.root / "plan.md"), "--session", "S1", expect=2)

    assert out["code"] == "TARGET_NOT_IN_SYNC", out
    assert out["details"]["relation"] == "diverged", out["details"]
    assert _side_effects(repo) == before


def test_b3_guard_ahead_start_succeeds(repo, cli, tmp_path):
    """不变量：本地在上游之上（ahead）时 start 成功，起点就是本地 ref。"""
    _setup_origin(repo, tmp_path)
    (repo.root / "local-only.txt").write_text("local\n", encoding="utf-8")
    repo.commit_all("chore(local): [cr_id_skip] Local only")
    local_head = _head(repo)

    out = cli("start", "--plan", str(repo.root / "plan.md"), "--session", "S1")

    assert out["target_start_head"] == local_head, out


# ---------------------------------------------------------------- B4：local_only / unverified


def test_b4_no_remote_is_local_only_with_warning(repo, cli):
    assert git(repo.root, "remote") == ""
    out = cli("start", "--plan", str(repo.root / "plan.md"), "--session", "S1")
    assert out["target_baseline"]["relation"] == "local_only", out.get("target_baseline")
    warning = out["target_baseline"]["warning"]
    assert isinstance(warning, str) and warning.strip(), out["target_baseline"]


def test_b4_remote_without_upstream_is_unverified(repo, cli, tmp_path):
    """边界：有 remote 但没有可用的上游跟踪 ref → unverified，同样成功且 warning 非空。"""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(origin)], check=True, capture_output=True)
    git(repo.root, "remote", "add", "origin", str(origin))  # 只加 remote，不建上游跟踪

    out = cli("start", "--plan", str(repo.root / "plan.md"), "--session", "S1")

    assert out["target_baseline"]["relation"] == "unverified", out.get("target_baseline")
    warning = out["target_baseline"]["warning"]
    assert isinstance(warning, str) and warning.strip(), out["target_baseline"]


def test_b4_guard_target_start_head_matches_local_ref(repo, cli):
    """不变量：target_start_head 仍等于本地 refs/heads/<target> 的 sha。"""
    out = cli("start", "--plan", str(repo.root / "plan.md"), "--session", "S1")
    assert out["target_start_head"] == _head(repo)


# ---------------------------------------------------------------- B5：rebase 与上游


def test_b5_rebase_blocks_when_upstream_ahead(repo, cli, tmp_path):
    origin = _setup_origin(repo, tmp_path)
    out = cli("start", "--plan", str(repo.root / "plan.md"), "--session", "S1")
    lp = Path(out["ledger"])
    start_head = out["target_start_head"]
    cand_head = L.load(lp)["candidate"]["head"]
    _advance_origin(repo, tmp_path, origin)

    err = cli("rebase", "--session", "S1", expect=1)

    assert err["code"] == "TARGET_NOT_IN_SYNC", err
    assert err["details"]["relation"] == "behind", err["details"]
    lg_after = L.load(lp)
    assert lg_after["repo"]["target_start_head"] == start_head
    assert lg_after["candidate"]["head"] == cand_head


def test_b5_guard_rebase_after_manual_fast_forward_updates_start_head(repo, cli, tmp_path):
    """边界（既有行为，起点上已成立）：本地 ref 被手动快进到上游后再 rebase → rebased true 且
    target_start_head 更新。新的上游检查不得在本地已追上后继续拦。"""
    origin = _setup_origin(repo, tmp_path)
    out = cli("start", "--plan", str(repo.root / "plan.md"), "--session", "S1")
    lp = Path(out["ledger"])
    old_head = out["target_start_head"]
    _advance_origin(repo, tmp_path, origin)
    git(repo.root, "merge", "-q", "--ff-only", "origin/main")
    new_head = _head(repo)
    assert new_head != old_head

    rb = cli("rebase", "--session", "S1")

    assert rb["rebased"] is True, rb
    assert L.load(lp)["repo"]["target_start_head"] == new_head


def test_b5_guard_rebase_without_upstream_change_unchanged(repo, cli, tmp_path):
    """不变量：上游未前进时 bl rebase 仍返回既有形状（rebased false）。"""
    _setup_origin(repo, tmp_path)
    out = cli("start", "--plan", str(repo.root / "plan.md"), "--session", "S1")

    rb = cli("rebase", "--session", "S1")

    assert rb["rebased"] is False, rb
    assert L.load(Path(out["ledger"]))["repo"]["target_start_head"] == out["target_start_head"]


# ---------------------------------------------------------------- B6：finalize 与上游


def test_b6_finalize_blocks_when_upstream_ahead(repo, cli, hook, tmp_path):
    origin = _setup_origin(repo, tmp_path)
    out = cli("start", "--plan", str(repo.root / "plan.md"), "--session", "S1")
    lp = Path(out["ledger"])
    start_head = out["target_start_head"]
    started = _started(out)
    drive_to_proof_pass(started, cli, hook)
    reviewer_pass(hook)
    assert cli("status", "--session", "S1")["readiness"]["next_action"] == "finalize"
    _advance_origin(repo, tmp_path, origin)

    err = cli("finalize", "--session", "S1", "-m", "feat(foo): [cr_id_skip] Add mul", expect=1)

    assert err["code"] == "TARGET_NOT_IN_SYNC", err
    assert _head(repo) == start_head  # refs/heads/main 未变
    assert L.load(lp)["finalize_intent"] is None


def test_b6_finalize_still_requires_sync_after_stale_ack(repo, cli, hook, tmp_path):
    """边界：start 时 --allow-stale-target 承认过的 run，finalize 仍要求与上游一致。"""
    origin = _setup_origin(repo, tmp_path)
    _advance_origin(repo, tmp_path, origin)
    out = cli("start", "--plan", str(repo.root / "plan.md"), "--session", "S1", "--allow-stale-target")
    assert out["target_baseline"]["acknowledged"] is True, out.get("target_baseline")
    started = _started(out)
    drive_to_proof_pass(started, cli, hook)
    reviewer_pass(hook)

    err = cli("finalize", "--session", "S1", "-m", "feat(foo): [cr_id_skip] Add mul", expect=1)

    assert err["code"] == "TARGET_NOT_IN_SYNC", err


def test_b6_guard_target_drift_still_reported(repo, cli, hook, tmp_path):
    """不变量：本地 ref 相对 target_start_head 动了仍报既有的 TARGET_DRIFT。"""
    _setup_origin(repo, tmp_path)
    out = cli("start", "--plan", str(repo.root / "plan.md"), "--session", "S1")
    started = _started(out)
    drive_to_proof_pass(started, cli, hook)
    reviewer_pass(hook)
    (repo.root / "README.md").write_text("drift\n", encoding="utf-8")
    repo.commit_all("docs(x): [cr_id_skip] Local drift")

    err = cli("finalize", "--session", "S1", "-m", "feat(foo): [cr_id_skip] Add mul", expect=1)

    assert err["code"] == "TARGET_DRIFT", err


# ---------------------------------------------------------------- B7：worktree.link_paths


def _link_ready(repo) -> Path:
    _write_loop(repo, _loop_text(link_paths=["node_modules"]))
    _write_ignore(repo, "node_modules/")
    repo.commit_all("chore(fixture): [cr_id_skip] Link dependency")
    dep = repo.root / "node_modules"
    dep.mkdir()
    (dep / "marker").write_text(MARKER + "\n", encoding="utf-8")
    return dep


def _readlink_target(link: Path) -> Path:
    target = Path(os.readlink(link))
    return target.resolve() if target.is_absolute() else (link.parent / target).resolve()


def test_b7_start_links_declared_path_into_both_worktrees(repo, cli):
    dep = _link_ready(repo)

    out = cli("start", "--plan", str(repo.root / "plan.md"), "--session", "S1")

    for wt in (Path(out["worktree"]), Path(out["tester_worktree"])):
        link = wt / "node_modules"
        assert link.is_symlink(), f"{link} 应是符号链接"
        assert _readlink_target(link) == dep.resolve(), (link, os.readlink(link))


def test_b7_missing_link_source_fails_without_side_effects(repo, cli):
    _write_loop(repo, _loop_text(link_paths=["node_modules"]))
    _write_ignore(repo, "node_modules/")
    repo.commit_all("chore(fixture): [cr_id_skip] Declared but missing dependency")
    assert not (repo.root / "node_modules").exists()
    before = _side_effects(repo)

    err = cli("start", "--plan", str(repo.root / "plan.md"), "--session", "S1", expect=2)

    assert err["code"] == "WORKTREE_LINK_SOURCE_MISSING", err
    assert _side_effects(repo) == before


def test_b7_tracked_path_is_not_replaced_by_link(repo, cli):
    """边界：已链接的未跟踪目录建链；git 已跟踪的同名路径不被链接覆盖。"""
    (repo.root / "vendor").mkdir()
    (repo.root / "vendor" / "keep.txt").write_text("tracked\n", encoding="utf-8")
    _write_loop(repo, _loop_text(link_paths=["node_modules", "vendor"]))
    _write_ignore(repo, "node_modules/")
    repo.commit_all("chore(fixture): [cr_id_skip] Link deps with tracked vendor")
    dep = repo.root / "node_modules"
    dep.mkdir()
    (dep / "marker").write_text(MARKER + "\n", encoding="utf-8")

    out = cli("start", "--plan", str(repo.root / "plan.md"), "--session", "S1")

    wt = Path(out["worktree"])
    assert (wt / "node_modules").is_symlink(), "未跟踪的依赖目录应被链接"
    assert _readlink_target(wt / "node_modules") == dep.resolve()
    vendor = wt / "vendor"
    assert not vendor.is_symlink(), "git 已跟踪的路径不得被链接覆盖"
    assert (vendor / "keep.txt").read_text(encoding="utf-8") == "tracked\n"


def test_b7_guard_loop_config_unchanged_without_link_paths(repo, cli):
    """边界：未声明 link_paths 时 ledger 的 loop_config JSON 与改动前逐字节一致。"""
    out = cli("start", "--plan", str(repo.root / "plan.md"), "--session", "S1")
    lg = _ledger(out)
    raw = (repo.root / ".claude" / "loop.yml").read_bytes()
    assert lg["loop_config"] == {
        "path": ".claude/loop.yml",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "pass_cmd": [{"stage": "test", "cmd": PYTEST_CMD, "timeout": 60}],
        "max_iterations": 3,
        "worktree_root": None,
        "proof_runner": {"framework": "pytest", "cmd": PROOF_RUNNER_CMD},
        "evidence_neutral_paths": [],
    }, lg["loop_config"]


def test_b7_guard_undeclared_link_paths_start_unchanged(repo, cli):
    """不变量：未声明 worktree.link_paths 的项目 start 行为不变（不生成任何链接）。"""
    dep = repo.root / "node_modules"
    dep.mkdir()
    (dep / "marker").write_text(MARKER + "\n", encoding="utf-8")

    out = cli("start", "--plan", str(repo.root / "plan.md"), "--session", "S1")

    for wt in (Path(out["worktree"]), Path(out["tester_worktree"])):
        assert not (wt / "node_modules").is_symlink()


# ---------------------------------------------------------------- B8：链接后的 preflight / machine / proof


def _start_linked_run(repo, cli) -> dict:
    _write_loop(repo, _loop_text(ready_cmd="test -d node_modules", link_paths=["node_modules"]))
    _write_ignore(repo, "node_modules/")
    repo.commit_all("chore(fixture): [cr_id_skip] Linked ready probe")
    dep = repo.root / "node_modules"
    dep.mkdir()
    (dep / "marker").write_text(MARKER + "\n", encoding="utf-8")
    return cli("start", "--plan", str(repo.root / "plan.md"), "--session", "S1")


def _run_bl_subprocess(cwd: Path, *args: str) -> tuple[int, str]:
    env = os.environ.copy()
    parts = [str(RUNTIME)]
    if env.get("PYTHONPATH"):
        parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(parts)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run([sys.executable, "-m", "builder_loop", *args], cwd=str(cwd),
                          env=env, capture_output=True, text=True)
    return proc.returncode, proc.stdout


@contextmanager
def _record_snapshot_link(monkeypatch):
    """在 preflight 的快照 worktree 被清理前记录 node_modules 是不是符号链接、指向哪。"""
    seen: dict = {}
    real = W.temp_worktree

    @contextmanager
    def wrapper(*args, **kwargs):
        with real(*args, **kwargs) as path:
            try:
                yield path
            finally:
                link = Path(path) / "node_modules"
                seen["islink"] = link.is_symlink()
                seen["target"] = os.path.realpath(link) if link.is_symlink() else None

    monkeypatch.setattr(W, "temp_worktree", wrapper)
    yield seen


def test_b8_preflight_and_machine_see_linked_dependency(repo, cli, monkeypatch):
    out = _start_linked_run(repo, cli)
    wt = Path(out["worktree"])
    dep = (repo.root / "node_modules").resolve()
    assert (wt / "node_modules").is_symlink()
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")

    with _record_snapshot_link(monkeypatch) as seen:
        pre = cli("preflight", "--session", "S1")
    assert pre["baseline_unready"] == [], pre
    assert seen.get("islink") is True, seen
    assert Path(seen["target"]) == dep, seen

    code, stdout = _run_bl_subprocess(repo.root, "machine", "--session", "S1")
    m = json.loads(stdout)
    assert code == 0, (code, stdout)
    assert m["ok"] is True, m
    assert m["result"] == "PASS", m
    assert "MACHINE_STAGE_UNREADY" not in stdout and "MACHINE_WORKTREE_CHANGED" not in stdout, stdout


def test_b8_candidate_link_survives_proof_counterexamples(repo, cli, hook):
    out = _start_linked_run(repo, cli)
    wt, twt = Path(out["worktree"]), Path(out["tester_worktree"])
    link = wt / "node_modules"
    target = _readlink_target(link)
    assert link.is_symlink()
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    write_mul_test(twt)
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    r = role_turn(hook, "tester", "T1", make_tester_result("mutation"), start=False)
    assert r["code"] == 0, r
    cli("integrate", "--session", "S1")
    assert cli("machine", "--session", "S1")["result"] == "PASS"
    r = role_turn(hook, "tester", "T1", make_tester_result("mutation", mutation_patch(wt)), start=False)
    assert r["code"] == 0, r
    proof_out = cli("proof", "--session", "S1")
    assert proof_out["result"] == "PASS", proof_out

    assert link.is_symlink(), "多组反例执行后候选里的链接仍应存在"
    assert _readlink_target(link) == target


def test_b8_guard_machine_stage_classification_unchanged_without_link_paths(repo, cli):
    """不变量：未声明 link_paths 时 machine 的 stage 判定与改动前一致。"""
    write_plan(repo.root, contract_with(**{"assurance.required": ["machine", "reviewer"]}), "lite.md")
    out = cli("start", "--plan", str(repo.root / "lite.md"), "--session", "S1")
    wt = Path(out["worktree"])
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")

    m = cli("machine", "--session", "S1")

    assert m["result"] == "PASS", m