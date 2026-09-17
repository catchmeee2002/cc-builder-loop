"""项目配置 `.claude/loop.yml`（与 cc-old 兼容）。

只消费 pass_cmd[] / max_iterations / worktree.root；其余字段忽略。
有 PyYAML 用 PyYAML；否则用内置扁平 YAML 子集解析（块映射、块序列、JSON 风格 flow 值）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import fatal
from .jsonutil import sha256_file

LOOP_YML = Path(".claude") / "loop.yml"
DEFAULT_MAX_ITERATIONS = 5
DEFAULT_STAGE_TIMEOUT = 300
PROOF_FRAMEWORKS = ("pytest", "generic")
# proof 的测试命令由项目声明、start 时冻结进 assurance 面：环境依赖因此进入 evidence 的输入投影，
# tester 不再自己写 argv（#229：否则只能把机器侧 venv 的绝对路径藏进 argv）。
DEFAULT_PROOF_RUNNER = {"framework": "pytest", "cmd": "python3 -m pytest"}


@dataclass
class Stage:
    stage: str
    cmd: str
    timeout: int = DEFAULT_STAGE_TIMEOUT

    def to_json(self) -> dict[str, Any]:
        return {"stage": self.stage, "cmd": self.cmd, "timeout": self.timeout}


@dataclass
class LoopConfig:
    path: str
    sha256: str
    pass_cmd: list[Stage] = field(default_factory=list)
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    worktree_root: str | None = None
    proof_runner: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_PROOF_RUNNER))

    def to_json(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "pass_cmd": [s.to_json() for s in self.pass_cmd],
            "max_iterations": self.max_iterations,
            "worktree_root": self.worktree_root,
            "proof_runner": self.proof_runner,
        }


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
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore

        return yaml.safe_load(text)
    except ImportError:
        return parse_yaml_subset(text)


# ---------------------------------------------------------------- loop.yml


def load_loop_config(repo_root: Path) -> LoopConfig:
    path = repo_root / LOOP_YML
    if not path.is_file():
        raise fatal("CONFIG_MISSING", f"缺少 {LOOP_YML}；先运行接入向导生成", path=str(path))
    try:
        data = load_yaml(path)
    except Exception as exc:  # noqa: BLE001 — 解析失败统一归 FATAL
        raise fatal("CONFIG_PARSE", f"{LOOP_YML} 解析失败: {exc}", path=str(path))
    if not isinstance(data, dict):
        raise fatal("CONFIG_PARSE", f"{LOOP_YML} 顶层必须是映射", path=str(path))

    raw_stages = data.get("pass_cmd")
    if not isinstance(raw_stages, list) or not raw_stages:
        raise fatal("CONFIG_PASS_CMD_EMPTY", "pass_cmd 必须是非空数组", path=str(path))
    stages: list[Stage] = []
    for idx, item in enumerate(raw_stages):
        if not isinstance(item, dict) or not item.get("stage") or not item.get("cmd"):
            raise fatal("CONFIG_PASS_CMD_INVALID", f"pass_cmd[{idx}] 需要 stage 和 cmd", item=item)
        timeout = item.get("timeout", DEFAULT_STAGE_TIMEOUT)
        if not isinstance(timeout, int) or timeout <= 0:
            raise fatal("CONFIG_PASS_CMD_INVALID", f"pass_cmd[{idx}].timeout 必须是正整数", item=item)
        stages.append(Stage(stage=str(item["stage"]), cmd=str(item["cmd"]), timeout=timeout))

    max_iter = data.get("max_iterations", DEFAULT_MAX_ITERATIONS)
    if not isinstance(max_iter, int) or max_iter <= 0:
        raise fatal("CONFIG_MAX_ITERATIONS_INVALID", "max_iterations 必须是正整数", value=max_iter)

    worktree_root = None
    wt = data.get("worktree")
    if isinstance(wt, dict) and wt.get("root"):
        worktree_root = str(wt["root"])

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

    return LoopConfig(
        proof_runner=runner,
        path=str(LOOP_YML),
        sha256=sha256_file(path),
        pass_cmd=stages,
        max_iterations=max_iter,
        worktree_root=worktree_root,
    )
