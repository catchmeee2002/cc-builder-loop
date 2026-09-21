"""B4：`bl machine` / `bl preflight` / `bl proof` 启动的子进程要拿到一个 run 目录之下、随调用
私有生成、用完即删的 PYTHONPYCACHEPREFIX——不读调用方环境里可能已有的值，也不让候选 worktree 里
陈旧的 `__pycache__/*.pyc` 泄漏进执行语义，同一 run 里连续两次调用互不相同。

冻结基线上 machine/preflight 的 env 构造（`runtime/builder_loop/machine.py::run_preflight` /
`run_machine`）完全不碰 PYTHONPYCACHEPREFIX：子进程原样继承调用方环境。这里的核心断言在起点代码上
会在 call 阶段直接失败（baseline-red）。
"""

from __future__ import annotations

import importlib.util
import py_compile
import struct
from pathlib import Path

from conftest import contract_with, implement_mul, mutation_patch, role_turn, make_tester_result, write_mul_test, write_plan


def _start_lite(repo, cli, session: str = "S1", required: list[str] | None = None):
    write_plan(repo.root, contract_with(**{"assurance.required": required or ["machine"]}), "lite.md")
    return cli("start", "--plan", str(repo.root / "lite.md"), "--session", session)


def poison_pycache(real_path: Path, mutated_source: str) -> Path:
    """把 real_path 对应的 __pycache__/*.pyc 换成 mutated_source 编译出的字节码，但把 pyc 头部
    记录的源 mtime/size 对齐到 real_path 当前的真实 stat：解释器如果直接信任本地缓存，就会加载
    mutated_source 的语义而不是 real_path 上真正的代码——这正是私有 PYTHONPYCACHEPREFIX 要防的事。"""
    real_text = real_path.read_text()
    assert len(mutated_source) == len(real_text), "mutated_source 必须与真实源码同字节数"
    cache_path = Path(importlib.util.cache_from_source(str(real_path)))
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_src = real_path.parent / (real_path.stem + "__mut__.py")
    tmp_pyc = real_path.parent / (real_path.stem + "__mut__.pyc")
    tmp_src.write_text(mutated_source)
    py_compile.compile(str(tmp_src), cfile=str(tmp_pyc), doraise=True,
                        invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP)
    st = real_path.stat()
    data = bytearray(tmp_pyc.read_bytes())
    struct.pack_into("<II", data, 8, int(st.st_mtime) & 0xFFFFFFFF, st.st_size & 0xFFFFFFFF)
    cache_path.write_bytes(bytes(data))
    tmp_src.unlink()
    tmp_pyc.unlink()
    return cache_path


ENVDUMP_SRC = "import os, sys\nopen(sys.argv[1], 'w').write(os.environ.get('PYTHONPYCACHEPREFIX', '<unset>'))\n"


def _start_with_envdump(repo, cli, marker: Path, extra_stage: str = "", session: str = "S1"):
    (repo.root / "envdump.py").write_text(ENVDUMP_SRC)
    (repo.root / ".claude" / "loop.yml").write_text(
        "pass_cmd:\n"
        f"  - stage: envdump\n    cmd: \"python3 envdump.py {marker}\"\n    timeout: 30\n"
        f"{extra_stage}"
        "max_iterations: 3\n"
        "proof_runner:\n  framework: pytest\n  cmd: \"python3 -m pytest -p no:html\"\n"
    )
    repo.commit_all()
    return _start_lite(repo, cli, session=session)


# ---------------------------------------------------------------- 陈旧 .pyc 不泄漏进语义


def test_stale_candidate_pycache_ignored_by_machine_and_proof(started, cli, hook, monkeypatch):
    monkeypatch.delenv("PYTHONPYCACHEPREFIX", raising=False)
    wt, twt = started["worktree"], started["tester_worktree"]
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")

    # 同字节数的变异源码：add 被改成减法、mul 被改成加法（都是单字符替换，长度不变）
    real = wt / "src" / "foo.py"
    original = real.read_text()
    mutated = original.replace("return a + b", "return a - b").replace("return a * b", "return a + b")
    assert mutated != original
    poisoned = poison_pycache(real, mutated)
    before_bytes = poisoned.read_bytes()

    write_mul_test(twt)
    assert role_turn(hook, "tester", "T1", make_tester_result("mutation"), start=False)["code"] == 0

    m = cli("machine", "--session", "S1")
    assert m["result"] == "PASS", m  # 若泄漏了陈旧字节码，add(1,2)==3 / mul(3,4)==12 都会算错
    assert poisoned.exists() and poisoned.read_bytes() == before_bytes  # 不删用户缓存、也不改它

    cli("integrate", "--session", "S1")
    assert role_turn(hook, "tester", "T1", make_tester_result("mutation", mutation_patch(wt)))["code"] == 0
    p = cli("proof", "--session", "S1")
    assert p["result"] == "PASS", p  # 候选阶段同样在 wt 上直接跑 pytest，同一条泄漏路径
    assert poisoned.exists() and poisoned.read_bytes() == before_bytes


# ---------------------------------------------------------------- PYTHONPYCACHEPREFIX 私有、每次不同、用完即删


def test_pythonpycacheprefix_private_per_call_and_ephemeral(repo, cli, monkeypatch, tmp_path):
    monkeypatch.setenv("PYTHONPYCACHEPREFIX", "/some/caller/prefix")
    marker = tmp_path / "prefix.txt"
    out = _start_with_envdump(repo, cli, marker)
    ledger_dir = str(Path(out["ledger"]).parent)

    pf = cli("preflight", "--session", "S1")
    assert pf["result"] == "GREEN"
    prefix_preflight = marker.read_text()
    assert prefix_preflight not in ("<unset>", "/some/caller/prefix")
    assert prefix_preflight.startswith(ledger_dir), prefix_preflight
    assert not Path(prefix_preflight).exists()  # 调用返回后目录已不存在

    (Path(out["worktree"]) / "src" / "scratch.py").write_text("")
    cli("checkpoint", "--session", "S1", "--role", "builder")
    m1 = cli("machine", "--session", "S1")
    assert m1["result"] == "PASS"
    prefix_m1 = marker.read_text()
    assert prefix_m1 not in ("<unset>", "/some/caller/prefix", prefix_preflight)
    assert prefix_m1.startswith(ledger_dir)
    assert not Path(prefix_m1).exists()

    m2 = cli("machine", "--session", "S1")
    assert m2["result"] == "PASS"
    prefix_m2 = marker.read_text()
    assert prefix_m2 not in ("<unset>", "/some/caller/prefix", prefix_preflight, prefix_m1)
    assert not Path(prefix_m2).exists()


def test_private_prefix_removed_even_after_stage_failure(repo, cli, tmp_path):
    marker = tmp_path / "prefix_fail.txt"
    out = _start_with_envdump(repo, cli, marker, extra_stage="  - stage: boom\n    cmd: \"false\"\n    timeout: 10\n")
    (Path(out["worktree"]) / "src" / "scratch.py").write_text("")
    cli("checkpoint", "--session", "S1", "--role", "builder")
    m = cli("machine", "--session", "S1", expect=1)
    assert m["result"] == "FAIL"
    prefix = marker.read_text()
    assert prefix != "<unset>"
    assert prefix.startswith(str(Path(out["ledger"]).parent))
    assert not Path(prefix).exists()  # stage 失败之后同样清理
