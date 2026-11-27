"""项目配置 `.claude/loop.yml`（与 V7.4 的格式兼容）。

只消费 pass_cmd[] / max_iterations / proof_runner / worktree.root；其余字段忽略。
有 PyYAML 用 PyYAML；否则用内置扁平 YAML 子集解析（块映射、块序列、JSON 风格 flow 值）。
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import fatal

LOOP_YML = Path(".claude") / "loop.yml"
DEFAULT_MAX_ITERATIONS = 5
DEFAULT_STAGE_TIMEOUT = 300
PROOF_FRAMEWORKS = ("pytest", "generic")
# preflight 只在 machine 失败时才有诊断价值；always 保住「这段本来就红吗」这个事实，是默认的稳妥选择。
# 三个模式都只影响「builder 是否被建议跑」，不影响手动 `bl preflight`（情报不是门禁，原则三）。
PREFLIGHT_MODES = ("always", "on-failure", "off")
DEFAULT_PREFLIGHT_MODE = "always"
# 并发预算：默认全串行，行为与引入这一项之前完全一致；用户按机器资源显式调高才生效（原则三、五）。
DEFAULT_CONCURRENCY = {"gates": 1, "proof_groups": 1}
# proof 的测试命令由项目声明、start 时冻结进 assurance 面：环境依赖因此进入 evidence 的输入投影，
# tester 不再自己写 argv（#229：否则只能把机器侧 venv 的绝对路径藏进 argv）。
DEFAULT_PROOF_RUNNER = {"framework": "pytest", "cmd": "python3 -m pytest"}
# 会被链接进反例现场的路径：relative to repo root, must stay inside it
LINK_PATHS_FIELD = "link_paths"
# pass_cmd 与 proof_runner 共用同一个占位符与同一个展开实现（#391，原则二）：
# 两条命令都在 worktree 里跑，主仓内的解释器只能靠它引进来。
MAIN_REPO_PLACEHOLDER = "{main_repo}"


def expand_main_repo(cmd: str, main_repo: Path) -> str:
    """把 cmd 里的 {main_repo} 换成主仓绝对路径；没有占位符时原样返回。"""
    return cmd.replace(MAIN_REPO_PLACEHOLDER, str(main_repo))


@dataclass
class Stage:
    stage: str
    cmd: str
    timeout: int = DEFAULT_STAGE_TIMEOUT
    # 环境就绪探针：非 0 / 超时说明环境没准备好，此时不跑 cmd，也不把结果记成判据（原则一，#377 #383）
    ready_cmd: str | None = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"stage": self.stage, "cmd": self.cmd, "timeout": self.timeout}
        if self.ready_cmd is not None:  # 不写该键时输出与旧格式逐字相同，既有 digest 不漂
            out["ready_cmd"] = self.ready_cmd
        return out


@dataclass
class LoopConfig:
    path: str
    sha256: str
    pass_cmd: list[Stage] = field(default_factory=list)
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    worktree_root: str | None = None
    proof_runner: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_PROOF_RUNNER))
    evidence_neutral_paths: list[str] = field(default_factory=list)
    # 依赖准备：run 起点把这些路径从 link_source 链接进候选 / tester / preflight 现场（#383）
    worktree_link_paths: list[str] = field(default_factory=list)
    worktree_link_source: str | None = None
    # 以下三项都是执行信息，不进 contract digest（原则四）：run 内改它们不该让既有 evidence 失效
    preflight_mode: str = DEFAULT_PREFLIGHT_MODE
    concurrency: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_CONCURRENCY))
    # 用户真正在 loop.yml 里显式写过的键（含 concurrency 子键）。to_json 与 retro / doctor 据此判断
    # 「这一项是用户选的还是默认值」，不另存一份事实（原则二）
    explicit_keys: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "path": self.path,
            "sha256": self.sha256,
            "pass_cmd": [s.to_json() for s in self.pass_cmd],
            "max_iterations": self.max_iterations,
            "worktree_root": self.worktree_root,
            "proof_runner": self.proof_runner,
            "evidence_neutral_paths": self.evidence_neutral_paths,
        }
        # 未声明时不写键：既有项目的 loop_config JSON 逐字节不变（与 Stage.to_json 同一约定）。
        # link_source 单独声明没有意义，与 link_paths 绑定写，避免只声明它时也改了 JSON 形状。
        if self.worktree_link_paths:
            out["worktree_link_paths"] = self.worktree_link_paths
            if self.worktree_link_source is not None:
                out["worktree_link_source"] = self.worktree_link_source
        # 同一约定：只写用户显式声明过的项。concurrency 只落显式子键，缺省子键读时回落到 DEFAULT_CONCURRENCY。
        if "preflight" in self.explicit_keys:
            out["preflight"] = self.preflight_mode
        conc = {k.split(".", 1)[1]: self.concurrency[k.split(".", 1)[1]]
                for k in self.explicit_keys if k.startswith("concurrency.")}
        if conc:
            out["concurrency"] = conc
        return out


# 可配项元数据：retro / doctor 据此告诉用户还有哪些能力没开（原则五：新能力必须能被已有用户发现）。
# 只放文案与默认值；「这一项该不该提示」由调用方按本 run 的实际观测判断，不在这里写死。
CONFIG_HINTS = (
    {"key": "preflight", "default": DEFAULT_PREFLIGHT_MODE,
     "summary": "preflight 默认每次 run 起点跑一遍全量；改 on-failure 只在 machine 失败时跑，能省一次全量"},
    {"key": "concurrency.gates", "default": DEFAULT_CONCURRENCY["gates"],
     "summary": "门禁并发默认 1（串行）；机器有余量时可调高，让 machine / proof 不再排队等 preflight"},
    {"key": "concurrency.proof_groups", "default": DEFAULT_CONCURRENCY["proof_groups"],
     "summary": "proof 反例段默认逐组串行；组数多时调高并发能显著缩短 proof"},
)


# ---------------------------------------------------------------- YAML 子集

_SCALAR_RE = re.compile(r"^(?P<key>[A-Za-z_][\w.-]*):(?:\s+(?P<val>.*))?$")


def _parse_flow(text: str) -> Any:
    """解析 `{a: 1, b: [x, "y"]}` 风格的 flow 值（JSON 超集：裸 key/值、单引号）。"""
    pos = 0

    def skip_ws() -> None:
        nonlocal pos
        while pos < len(text) and text[pos] in " \t\n":
            pos += 1

    def parse_value() -> Any:
        nonlocal pos
        skip_ws()
        if pos >= len(text):
            raise ValueError("unexpected end")
        ch = text[pos]
        if ch == "{":
            pos += 1
            obj: dict[str, Any] = {}
            while True:
                skip_ws()
                if text[pos] == "}":
                    pos += 1
                    return obj
                key = parse_value()
                skip_ws()
                if text[pos] != ":":
                    raise ValueError("expected ':'")
                pos += 1
                obj[str(key)] = parse_value()
                skip_ws()
                if text[pos] == ",":
                    pos += 1
        if ch == "[":
            pos += 1
            arr: list[Any] = []
            while True:
                skip_ws()
                if text[pos] == "]":
                    pos += 1
                    return arr
                arr.append(parse_value())
                skip_ws()
                if text[pos] == ",":
                    pos += 1
        if ch in ("'", '"'):
            end = text.index(ch, pos + 1)
            val = text[pos + 1:end]
            pos = end + 1
            return val
        start = pos
        while pos < len(text) and text[pos] not in ",}]:":
            pos += 1
        return _parse_scalar(text[start:pos])

    val = parse_value()
    skip_ws()
    if pos != len(text):
        raise ValueError("trailing characters")
    return val


def _parse_scalar(text: str) -> Any:
    text = text.strip()
    if text == "" or text == "~" or text == "null":
        return None
    if text in ("true", "True"):
        return True
    if text in ("false", "False"):
        return False
    if (text.startswith("[") and text.endswith("]")) or (text.startswith("{") and text.endswith("}")):
        return _parse_flow(text)
    if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
        return text[1:-1]
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d+\.\d+", text):
        return float(text)
    return text


def _strip_comment(line: str) -> str:
    # 只去掉不在引号内的 " #" 注释
    out, quote = [], None
    for i, ch in enumerate(line):
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            continue
        if ch == "#" and (i == 0 or line[i - 1].isspace()):
            break
        out.append(ch)
    return "".join(out).rstrip()


def parse_yaml_subset(text: str) -> Any:
    lines: list[tuple[int, str]] = []
    for raw in text.splitlines():
        stripped = _strip_comment(raw)
        if not stripped.strip():
            continue
        indent = len(stripped) - len(stripped.lstrip(" "))
        lines.append((indent, stripped.strip()))

    def parse_block(i: int, indent: int) -> tuple[Any, int]:
        if i >= len(lines):
            return None, i
        if lines[i][1].startswith("- "):
            return parse_seq(i, indent)
        return parse_map(i, indent)

    def parse_map(i: int, indent: int) -> tuple[dict[str, Any], int]:
        result: dict[str, Any] = {}
        while i < len(lines) and lines[i][0] == indent and not lines[i][1].startswith("- "):
            m = _SCALAR_RE.match(lines[i][1])
            if not m:
                raise fatal("CONFIG_PARSE", f"无法解析行: {lines[i][1]!r}")
            key, val = m.group("key"), m.group("val")
            i += 1
            if val is None or val.strip() == "":
                if i < len(lines) and lines[i][0] > indent:
                    result[key], i = parse_block(i, lines[i][0])
                else:
                    result[key] = None
            else:
                result[key] = _parse_scalar(val)
        return result, i

    def parse_seq(i: int, indent: int) -> tuple[list[Any], int]:
        result: list[Any] = []
        while i < len(lines) and lines[i][0] == indent and lines[i][1].startswith("- "):
            item_text = lines[i][1][2:].strip()
            i += 1
            m = _SCALAR_RE.match(item_text)
            if m and not item_text.startswith(("[", "{", '"', "'")):
                # 序列项是映射：首行 "- key: val"，后续行缩进更深
                item: dict[str, Any] = {}
                val = m.group("val")
                item[m.group("key")] = _parse_scalar(val) if val is not None else None
                if i < len(lines) and lines[i][0] > indent and not lines[i][1].startswith("- "):
                    rest, i = parse_map(i, lines[i][0])
                    item.update(rest)
                result.append(item)
            else:
                result.append(_parse_scalar(item_text))
        return result, i

    value, _ = parse_block(0, lines[0][0] if lines else 0)
    return value


def load_yaml(path: Path) -> Any:
    return load_yaml_text(path.read_text(encoding="utf-8"))


def load_yaml_text(text: str) -> Any:
    try:
        import yaml  # type: ignore

        return yaml.safe_load(text)
    except ImportError:
        return parse_yaml_subset(text)


# ---------------------------------------------------------------- loop.yml


def _parse_link_paths(raw: Any, field_name: str = "proof_runner.link_paths",
                      error_code: str = "CONFIG_PROOF_RUNNER_INVALID") -> list[str]:
    """链接路径：仓库内相对路径数组（正斜杠）。校验靠输入条件，不靠事后特判（原则四）。

    proof 的现场链接与 run 起点的依赖准备共用这一个校验（原则二：一个判定只有一个实现）。
    """
    if not isinstance(raw, list) or not all(isinstance(p, str) for p in raw):
        raise fatal(error_code,
                    f"{field_name} 必须是字符串数组（相对仓库根的路径，如 node_modules）", value=raw)
    out: list[str] = []
    for item in raw:
        text = item.strip().replace("\\", "/")
        if not text:
            raise fatal(error_code, f"{field_name} 不能有空串", value=raw)
        if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
            raise fatal(error_code, f"{field_name} 必须是相对仓库根的路径: {item}", value=raw)
        parts = [p for p in text.split("/") if p not in ("", ".")]
        if not parts:
            raise fatal(error_code, f"{field_name} 不能指向仓库根: {item}", value=raw)
        if ".." in parts:
            raise fatal(error_code, f"{field_name} 不能含 `..` 段: {item}", value=raw)
        if parts[0] == ".git":
            raise fatal(error_code, f"{field_name} 不能指向 .git: {item}", value=raw)
        out.append("/".join(parts))
    return out


def _parse_link_source(raw: Any) -> str:
    """worktree.link_source：依赖源目录，相对仓库根（默认 "." 即仓库根本身）。"""
    field_name = "worktree.link_source"
    if not isinstance(raw, str) or not raw.strip():
        raise fatal("CONFIG_WORKTREE_LINK_INVALID", f"{field_name} 必须是非空字符串（相对仓库根的路径）", value=raw)
    text = raw.strip().replace("\\", "/")
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        raise fatal("CONFIG_WORKTREE_LINK_INVALID", f"{field_name} 必须是相对仓库根的路径: {raw}", value=raw)
    parts = [p for p in text.split("/") if p not in ("", ".")]
    if not parts:
        return "."
    if ".." in parts:
        raise fatal("CONFIG_WORKTREE_LINK_INVALID", f"{field_name} 不能含 `..` 段: {raw}", value=raw)
    if parts[0] == ".git":
        raise fatal("CONFIG_WORKTREE_LINK_INVALID", f"{field_name} 不能指向 .git: {raw}", value=raw)
    return "/".join(parts)


def load_loop_config(repo_root: Path) -> LoopConfig:
    path = repo_root / LOOP_YML
    if not path.is_file():
        raise fatal("CONFIG_MISSING", f"缺少 {LOOP_YML}；先运行接入向导生成", path=str(path))
    return _parse_loop_config(path.read_bytes(), str(path))


def load_loop_config_at(repo_root: Path, commit: str) -> LoopConfig | None:
    """某个提交里跟踪的 loop.yml；该提交没有跟踪它时返回 None（项目不把 loop.yml 纳入版本控制）。"""
    from . import gitx

    if not gitx.ls_tree_blobs(repo_root, commit, [LOOP_YML.as_posix()]):
        return None
    r = subprocess.run(["git", "cat-file", "blob", f"{commit}:{LOOP_YML.as_posix()}"], cwd=str(repo_root), capture_output=True, check=False)
    if r.returncode != 0:
        # 树里有这个路径却读不出来：不能让调用方静默回退到工作区那份
        raise fatal("CONFIG_UNREADABLE", f"无法读取 {commit[:12]}:{LOOP_YML.as_posix()}",
                    path=f"{commit[:12]}:{LOOP_YML.as_posix()}", stderr=r.stderr.decode("utf-8", "replace")[-500:])
    return _parse_loop_config(r.stdout, f"{commit[:12]}:{LOOP_YML.as_posix()}")


def _parse_loop_config(raw: bytes, path: str) -> LoopConfig:
    try:
        data = load_yaml_text(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 — 解析失败统一归 FATAL
        raise fatal("CONFIG_PARSE", f"{LOOP_YML} 解析失败: {exc}", path=path)
    if not isinstance(data, dict):
        raise fatal("CONFIG_PARSE", f"{LOOP_YML} 顶层必须是映射", path=path)

    raw_stages = data.get("pass_cmd")
    if not isinstance(raw_stages, list) or not raw_stages:
        raise fatal("CONFIG_PASS_CMD_EMPTY", "pass_cmd 必须是非空数组", path=path)
    stages: list[Stage] = []
    for idx, item in enumerate(raw_stages):
        if not isinstance(item, dict) or not item.get("stage") or not item.get("cmd"):
            raise fatal("CONFIG_PASS_CMD_INVALID", f"pass_cmd[{idx}] 需要 stage 和 cmd", item=item)
        timeout = item.get("timeout", DEFAULT_STAGE_TIMEOUT)
        if not isinstance(timeout, int) or timeout <= 0:
            raise fatal("CONFIG_PASS_CMD_INVALID", f"pass_cmd[{idx}].timeout 必须是正整数", item=item)
        ready_cmd = item.get("ready_cmd")
        if ready_cmd is not None and (not isinstance(ready_cmd, str) or not ready_cmd.strip()):
            raise fatal("CONFIG_PASS_CMD_INVALID", f"pass_cmd[{idx}].ready_cmd 必须是非空字符串", item=item)
        stages.append(Stage(stage=str(item["stage"]), cmd=str(item["cmd"]), timeout=timeout,
                            ready_cmd=ready_cmd.strip() if ready_cmd is not None else None))

    max_iter = data.get("max_iterations", DEFAULT_MAX_ITERATIONS)
    if not isinstance(max_iter, int) or max_iter <= 0:
        raise fatal("CONFIG_MAX_ITERATIONS_INVALID", "max_iterations 必须是正整数", value=max_iter)

    worktree_root = None
    worktree_link_paths: list[str] = []
    worktree_link_source: str | None = None
    wt = data.get("worktree")
    if isinstance(wt, dict) and wt.get("root"):
        worktree_root = str(wt["root"])
    if isinstance(wt, dict) and wt.get(LINK_PATHS_FIELD) is not None:
        worktree_link_paths = _parse_link_paths(wt[LINK_PATHS_FIELD], f"worktree.{LINK_PATHS_FIELD}",
                                                    "CONFIG_WORKTREE_LINK_INVALID")
    if isinstance(wt, dict) and wt.get("link_source") is not None:
        worktree_link_source = _parse_link_source(wt["link_source"])

    runner = dict(DEFAULT_PROOF_RUNNER)
    raw_runner = data.get("proof_runner")
    if raw_runner is not None:
        if not isinstance(raw_runner, dict):
            raise fatal("CONFIG_PROOF_RUNNER_INVALID", "proof_runner 必须是映射 {framework, cmd}", value=raw_runner)
        framework = raw_runner.get("framework", "pytest")
        if framework not in PROOF_FRAMEWORKS:
            raise fatal("CONFIG_PROOF_RUNNER_INVALID", f"proof_runner.framework 必须是 {PROOF_FRAMEWORKS}", value=framework)
        cmd = raw_runner.get("cmd") or (DEFAULT_PROOF_RUNNER["cmd"] if framework == "pytest" else None)
        if not isinstance(cmd, str) or not cmd.strip():
            raise fatal("CONFIG_PROOF_RUNNER_INVALID", "proof_runner.cmd 不能为空（generic 必须显式给出）")
        runner = {"framework": framework, "cmd": cmd.strip()}
        raw_links = raw_runner.get(LINK_PATHS_FIELD)
        if raw_links is not None:
            runner[LINK_PATHS_FIELD] = _parse_link_paths(raw_links)

    # 对判据中性的路径：pass_cmd / proof_runner 读不到它们，改它们不让 machine / proof 失效。
    # 是项目事实（这个仓库的判据读什么），不是单次任务事实，所以只在 loop.yml 声明（原则二）。
    raw_neutral = data.get("evidence_neutral_paths")
    neutral: list[str] = []
    if raw_neutral is not None:
        if not isinstance(raw_neutral, list) or not all(isinstance(p, str) and p.strip() for p in raw_neutral):
            raise fatal("CONFIG_EVIDENCE_NEUTRAL_PATHS_INVALID",
                        "evidence_neutral_paths 必须是字符串数组，每个元素都不能是空串（glob，相对仓库根，如 docs/**）；"
                        "整个数组为空或字段缺失都合法，表示不豁免任何路径", value=raw_neutral)
        neutral = sorted({p.strip() for p in raw_neutral})

    # 执行信息（原则四）：只影响等待与调度，不影响判据强度，因此不进 assurance 面
    explicit_keys: list[str] = []
    raw_preflight = data.get("preflight")
    preflight_mode = DEFAULT_PREFLIGHT_MODE
    if raw_preflight is not None:
        if isinstance(raw_preflight, bool):
            # YAML 1.1 把 off/on 解析成布尔（PyYAML 这样，内置子集解析不会）：`preflight: off` 要按 off 理解
            raw_preflight = DEFAULT_PREFLIGHT_MODE if raw_preflight else "off"
        if raw_preflight not in PREFLIGHT_MODES:
            raise fatal("CONFIG_PREFLIGHT_INVALID", f"preflight 必须是 {PREFLIGHT_MODES} 之一", value=raw_preflight)
        preflight_mode = raw_preflight
        explicit_keys.append("preflight")

    concurrency = dict(DEFAULT_CONCURRENCY)
    raw_conc = data.get("concurrency")
    if raw_conc is not None:
        if not isinstance(raw_conc, dict):
            raise fatal("CONFIG_CONCURRENCY_INVALID", "concurrency 必须是映射 {gates, proof_groups}", value=raw_conc)
        for name in ("gates", "proof_groups"):
            if name not in raw_conc:
                continue
            val = raw_conc[name]
            # bool 是 int 的子类，显式排除，否则 `gates: true` 会被当成 1
            if not isinstance(val, int) or isinstance(val, bool) or val < 1:
                raise fatal("CONFIG_CONCURRENCY_INVALID", f"concurrency.{name} 必须是 >=1 的整数", value=val)
            concurrency[name] = val
            explicit_keys.append(f"concurrency.{name}")

    return LoopConfig(
        proof_runner=runner,
        path=str(LOOP_YML),
        sha256=hashlib.sha256(raw).hexdigest(),
        pass_cmd=stages,
        max_iterations=max_iter,
        worktree_root=worktree_root,
        evidence_neutral_paths=neutral,
        worktree_link_paths=worktree_link_paths,
        worktree_link_source=worktree_link_source,
        preflight_mode=preflight_mode,
        concurrency=concurrency,
        explicit_keys=explicit_keys,
    )
