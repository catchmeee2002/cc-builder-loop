"""B7：`bl doctor` 输出 config 观测面。

given 仓库存在可解析的 .claude/loop.yml / when 调 doctor(repo_arg) / then 返回 dict 含 config 字段，
explicit_keys 为字符串数组，current.preflight 为 preflight_mode 生效值，current.concurrency 含
gates 与 proof_groups。起点 doctor 返回里没有 config，取它会 KeyError → call 阶段失败（baseline-red）。

边界（loop.yml 缺失 / 不可解析 → config 含 error 且 doctor 不崩溃）与不变量（doctor 既有字段与
problems 判定不变）作为守卫留在同一文件，不列入 proof group。
"""

from __future__ import annotations

from builder_loop.doctor import doctor

from keypath_helpers import loop_yml


def test_b7_doctor_reports_effective_config(repo):
    (repo.root / ".claude" / "loop.yml").write_text(
        loop_yml(preflight="off", gates=2, proof_groups=2), encoding="utf-8")
    out = doctor(str(repo.root))
    cfg = out["config"]
    assert isinstance(cfg["explicit_keys"], list), cfg
    assert all(isinstance(k, str) for k in cfg["explicit_keys"]), cfg
    assert cfg["current"]["preflight"] == "off", cfg
    conc = cfg["current"]["concurrency"]
    assert conc["gates"] == 2, cfg
    assert conc["proof_groups"] == 2, cfg


def test_b7_doctor_default_config_surface(repo):
    out = doctor(str(repo.root))
    cfg = out["config"]
    assert isinstance(cfg["explicit_keys"], list), cfg
    assert cfg["current"]["preflight"] == "always", cfg
    assert "gates" in cfg["current"]["concurrency"], cfg
    assert "proof_groups" in cfg["current"]["concurrency"], cfg


# ---------------------------------------------------------------- 边界（守卫）


def test_b7_boundary_missing_loop_yml_reports_error(repo):
    (repo.root / ".claude" / "loop.yml").unlink()
    out = doctor(str(repo.root))
    assert out["config"].get("error"), out.get("config")


def test_b7_boundary_unparseable_loop_yml_reports_error(repo):
    (repo.root / ".claude" / "loop.yml").write_text("pass_cmd: [\n", encoding="utf-8")
    out = doctor(str(repo.root))
    assert out["config"].get("error"), out.get("config")


# ---------------------------------------------------------------- 不变量（守卫）


def test_b7_invariant_existing_doctor_fields_and_problems_unchanged(repo):
    out = doctor(str(repo.root))
    for key in ("python", "git", "hooks", "sessions", "broken_symlinks", "problems", "healthy"):
        assert key in out, key
    assert isinstance(out["problems"], list), out
    assert out["healthy"] is (not out["problems"]), out