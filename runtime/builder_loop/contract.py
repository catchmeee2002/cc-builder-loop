"""contract：plan.md 里 `<!-- builder-loop-contract -->` 标签内的 JSON。

三个事实面各自算 canonical digest：mission（语义）/ authority（写边界）/ assurance（判据）。
mission 变 → revision+1 且需用户确认；authority 扩大、assurance 降级需用户授权。
`assurance.machine_commands` 不由 planner 写，start 时从 loop.yml 冻结进来。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .config import LoopConfig
from .errors import fatal
from .jsonutil import digest, sha256_file

CONTRACT_SCHEMA = "builder-loop/contract@1"
CONTRACT_TAG = "builder-loop-contract"
FACETS = ("mission", "authority", "assurance")
EVIDENCE_KINDS = ("machine", "tester", "proof", "reviewer")
PROOF_KINDS = ("baseline-red", "mutation", "reviewed-boundaries")

# 每个 behavior 的 proof 下限：缺省 strong（只许 baseline-red / mutation）；
# 只有 contract 在该 behavior 上显式写 "reviewed-boundaries"，tester 才能选最弱的 kind。
PROOF_FLOOR_STRONG = "strong"
PROOF_FLOOR_REVIEWED = "reviewed-boundaries"
PROOF_FLOORS = (PROOF_FLOOR_STRONG, PROOF_FLOOR_REVIEWED)

# runner / 构建控制面：冻结的是「规则」（basename 集合）而不是文件列表——新建的 conftest.py 同样能
# 劫持测试收集，monorepo 下文件列表还会让 digest 依赖树状态。
CONTROL_BASENAMES = (
    "pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini", "noxfile.py", "conftest.py", "Makefile",
    "package.json", "go.mod", "Cargo.toml", "BUILD", "BUILD.bazel", "WORKSPACE", "loop.yml",
)

OWNER_BUILDER = "builder"
OWNER_TESTER = "tester"

CHANGE_MISSION = "MISSION_REVISION"
CHANGE_AUTHORITY_EXPAND = "AUTHORITY_EXPAND"
CHANGE_ASSURANCE_DOWNGRADE = "ASSURANCE_DOWNGRADE"
CHANGE_NEUTRAL = "NEUTRAL"


# ---------------------------------------------------------------- 标签提取


def extract_tag(text: str, name: str) -> str | None:
    """`<!-- name -->` … `<!-- /name -->` 之间的内容；恰好一处才合法。"""
    pattern = re.compile(rf"<!--\s*{re.escape(name)}\s*-->(.*?)<!--\s*/{re.escape(name)}\s*-->", re.DOTALL)
    matches = pattern.findall(text)
    if not matches:
        return None
    if len(matches) > 1:
        raise fatal("CONTRACT_DUPLICATE", f"plan 中出现多处 <!-- {name} --> 标签", count=len(matches))
    return matches[0]


def _strip_fence(body: str) -> str:
    body = body.strip()
    m = re.match(r"^```[A-Za-z0-9_-]*\s*\n(.*?)\n```\s*$", body, re.DOTALL)
    return m.group(1) if m else body


def parse_contract_text(plan_text: str) -> dict[str, Any]:
    body = extract_tag(plan_text, CONTRACT_TAG)
    if body is None:
        raise fatal("CONTRACT_MISSING", f"plan 缺少 <!-- {CONTRACT_TAG} --> 标签")
    try:
        contract = json.loads(_strip_fence(body))
    except json.JSONDecodeError as exc:
        raise fatal("CONTRACT_PARSE", f"contract JSON 解析失败: {exc}")
    validate_contract(contract)
    return contract


def parse_contract_file(plan_path: Path) -> dict[str, Any]:
    if not plan_path.is_file():
        raise fatal("PLAN_NOT_FOUND", f"plan 文件不存在: {plan_path}", path=str(plan_path))
    return parse_contract_text(plan_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 校验


def _require(obj: dict[str, Any], key: str, typ: type, where: str) -> Any:
    if key not in obj:
        raise fatal("CONTRACT_INVALID", f"{where}.{key} 缺失")
    if not isinstance(obj[key], typ):
        raise fatal("CONTRACT_INVALID", f"{where}.{key} 类型应为 {typ.__name__}")
    return obj[key]


def validate_contract(contract: dict[str, Any]) -> None:
    if not isinstance(contract, dict):
        raise fatal("CONTRACT_INVALID", "contract 必须是 JSON 对象")
    if contract.get("schema") != CONTRACT_SCHEMA:
        raise fatal("CONTRACT_INVALID", f"schema 必须是 {CONTRACT_SCHEMA}", got=contract.get("schema"))
    for facet in FACETS:
        _require(contract, facet, dict, "contract")

    m = contract["mission"]
    revision = _require(m, "revision", int, "mission")
    if revision < 1:
        raise fatal("CONTRACT_INVALID", "mission.revision 必须 ≥ 1")
    slug = _require(m, "slug", str, "mission")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,48}", slug):
        raise fatal("CONTRACT_INVALID", "mission.slug 必须是小写字母/数字/连字符，2–49 字符", slug=slug)
    _require(m, "objective", str, "mission")
    behaviors = _require(m, "behaviors", list, "mission")
    if not behaviors:
        raise fatal("CONTRACT_INVALID", "mission.behaviors 不能为空")
    seen: set[str] = set()
    for i, b in enumerate(behaviors):
        if not isinstance(b, dict) or not isinstance(b.get("id"), str) or not b["id"]:
            raise fatal("CONTRACT_INVALID", f"mission.behaviors[{i}].id 缺失")
        if b["id"] in seen:
            raise fatal("CONTRACT_INVALID", f"behavior id 重复: {b['id']}")
        seen.add(b["id"])
        for k in ("given", "when", "then"):
            if k not in b or not isinstance(b[k], str):
                raise fatal("CONTRACT_INVALID", f"mission.behaviors[{i}].{k} 缺失")
        # tester 从冻结基线盲写测试，contract 是它的唯一输入——边界与不变量写在这里
        for k in ("boundaries", "invariants"):
            if k in b and not (isinstance(b[k], list) and all(isinstance(x, str) for x in b[k])):
                raise fatal("CONTRACT_INVALID", f"mission.behaviors[{i}].{k} 应为字符串数组")
        if b.get("proof", PROOF_FLOOR_STRONG) not in PROOF_FLOORS:
            raise fatal("CONTRACT_INVALID", f"mission.behaviors[{i}].proof 必须是 {PROOF_FLOORS}", got=b.get("proof"))
    if "mock_strategy" in m and not isinstance(m["mock_strategy"], dict):
        raise fatal("CONTRACT_INVALID", "mission.mock_strategy 应为映射（依赖名 → mock 方式）")
    for k in ("interfaces", "acceptance_cases", "trust_boundaries"):
        m.setdefault(k, [])
        if not isinstance(m[k], list):
            raise fatal("CONTRACT_INVALID", f"mission.{k} 应为数组")

    a = contract["authority"]
    bw = _require(a, "builder_write", list, "authority")
    if not bw or not all(isinstance(p, str) and p for p in bw):
        raise fatal("CONTRACT_INVALID", "authority.builder_write 必须是非空字符串数组")
    a.setdefault("tester_write", [])
    a.setdefault("protected_paths", [])
    a.setdefault("target_branch", None)
    for k in ("tester_write", "protected_paths"):
        if not isinstance(a[k], list) or not all(isinstance(p, str) for p in a[k]):
            raise fatal("CONTRACT_INVALID", f"authority.{k} 应为字符串数组")
    if a["target_branch"] is not None and not isinstance(a["target_branch"], str):
        raise fatal("CONTRACT_INVALID", "authority.target_branch 应为字符串")
    # tester 地盘内的路径永远归 tester（path_owner 的优先级）。builder_write 若在这里点名，
    # 两个角色会对同一文件得到相反的归属结论，且谁都不敢动（#240）。规划期就拒掉。
    swallowed = [p for p in bw if glob_covers(a["tester_write"], p)]
    if swallowed:
        raise fatal(
            "CONTRACT_INVALID",
            "authority.builder_write 的这些条目落在 tester_write 之内：这些路径归 tester，builder 改不了。"
            "需要它们跟着实现变，就在对应 behavior 里写明要改成什么，由 tester 来改",
            paths=swallowed,
            tester_write=a["tester_write"],
        )

    s = contract["assurance"]
    req = _require(s, "required", list, "assurance")
    if not req:
        raise fatal("CONTRACT_INVALID", "assurance.required 不能为空")
    for k in req:
        if k not in EVIDENCE_KINDS:
            raise fatal("CONTRACT_INVALID", f"未知 evidence 种类: {k}", allowed=list(EVIDENCE_KINDS))
    if "machine" not in req:
        raise fatal("CONTRACT_INVALID", "assurance.required 必须包含 machine")
    if "proof" in req and "tester" not in req:
        raise fatal("CONTRACT_INVALID", "proof 依赖 tester，required 必须同时包含 tester")
    if "tester" in req and not a["tester_write"]:
        raise fatal("CONTRACT_INVALID", "required 含 tester 时 authority.tester_write 不能为空")
    s.setdefault("proof_kinds", list(PROOF_KINDS))
    s.setdefault("review_focus", [])
    for k in s["proof_kinds"]:
        if k not in PROOF_KINDS:
            raise fatal("CONTRACT_INVALID", f"未知 proof kind: {k}", allowed=list(PROOF_KINDS))
    if not isinstance(s["review_focus"], list):
        raise fatal("CONTRACT_INVALID", "assurance.review_focus 应为数组")
    if "machine_commands" in s and not isinstance(s["machine_commands"], list):
        raise fatal("CONTRACT_INVALID", "assurance.machine_commands 应为数组（通常由 start 冻结）")


# ---------------------------------------------------------------- 路径模式


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    if pattern.endswith("/"):
        pattern = pattern + "**"
    out = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        if ch == "*":
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(ch))
        i += 1
    return re.compile("^" + "".join(out) + "$")


def path_matches(pattern: str, path: str) -> bool:
    return bool(_glob_to_regex(pattern).match(path))


def path_in(patterns: list[str], path: str) -> bool:
    return any(path_matches(p, path) for p in patterns)


def has_wildcard(pattern: str) -> bool:
    return any(ch in pattern for ch in "*?[")


def glob_covers(old_patterns: list[str], new_pattern: str) -> bool:
    """旧模式集合是否覆盖新模式（保守：新模式含通配时只认完全相等或前缀 `/**` 覆盖）。"""
    for old in old_patterns:
        if old == new_pattern or old in ("**", "**/*"):
            return True
        if old.endswith("/**") and (new_pattern == old[:-3] or new_pattern.startswith(old[:-3] + "/")):
            return True
        if not has_wildcard(new_pattern) and path_matches(old, new_pattern):
            return True
    return False


# ---------------------------------------------------------------- 路径归属（唯一判定入口）


def path_owner(authority: dict[str, Any], path: str) -> str | None:
    """tester_write 优先：两边 glob 都命中时归 tester，写边界在构造上不相交，无需 glob 代数。"""
    if path_in(authority.get("tester_write", []), path):
        return OWNER_TESTER
    if path_in(authority.get("builder_write", []), path):
        return OWNER_BUILDER
    return None


def _literally_named(authority: dict[str, Any], path: str) -> bool:
    return any(p == path for key in ("builder_write", "tester_write") for p in authority.get(key, []))


def write_rejection(authority: dict[str, Any], role: str, path: str) -> str | None:
    """role 写 path 被拒的原因；None = 允许。checkpoint / PreToolUse / mutation patch 校验共用。"""
    if path_in(authority.get("protected_paths", []), path):
        return "protected"
    owner = path_owner(authority, path)
    if owner is None:
        return "outside_authority"
    if owner != role:
        return f"{owner}_owned"
    # 控制面文件：只拦 builder 靠 glob 顺带命中的情形；字面点名 = 计划明确授权。tester_write 内的归 tester。
    basename = path.rsplit("/", 1)[-1]
    if role == OWNER_BUILDER and basename in authority.get("control_basenames", []) and not _literally_named(authority, path):
        return "control_file"
    return None


def freeze_authority(contract: dict[str, Any]) -> dict[str, Any]:
    frozen = json.loads(json.dumps(contract))
    frozen["authority"]["control_basenames"] = list(CONTROL_BASENAMES)
    return frozen


# ---------------------------------------------------------------- digest 与变更分类


def facet_digests(contract: dict[str, Any]) -> dict[str, str]:
    return {facet: digest(contract[facet]) for facet in FACETS}


def freeze_assurance(contract: dict[str, Any], loop_config: LoopConfig) -> dict[str, Any]:
    frozen = freeze_authority(contract)
    frozen["assurance"]["machine_commands"] = [s.to_json() for s in loop_config.pass_cmd]
    frozen["assurance"]["max_iterations"] = loop_config.max_iterations
    frozen["assurance"]["proof_runner"] = dict(loop_config.proof_runner)
    return frozen


def classify_change(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    kinds: list[str] = []
    if digest(old["mission"]) != digest(new["mission"]):
        kinds.append(CHANGE_MISSION)

    oa, na = old["authority"], new["authority"]
    expands = False
    for key in ("builder_write", "tester_write"):
        for p in na.get(key, []):
            if not glob_covers(oa.get(key, []), p):
                expands = True
    if oa.get("target_branch") != na.get("target_branch"):
        expands = True
    if set(oa.get("protected_paths", [])) - set(na.get("protected_paths", [])):
        expands = True  # 保护集缩小 = 写权限扩大
    if set(oa.get("control_basenames", [])) - set(na.get("control_basenames", [])):
        expands = True
    if expands:
        kinds.append(CHANGE_AUTHORITY_EXPAND)

    os_, ns = old["assurance"], new["assurance"]
    downgrade = not set(os_.get("required", [])).issubset(set(ns.get("required", [])))
    old_cmds = {c["stage"]: c for c in os_.get("machine_commands", [])}
    new_cmds = {c["stage"]: c for c in ns.get("machine_commands", [])}
    for stage, cmd in old_cmds.items():
        if new_cmds.get(stage) != cmd:
            downgrade = True
    if set(os_.get("proof_kinds", [])) - set(ns.get("proof_kinds", [])) and "proof" in ns.get("required", []):
        downgrade = True
    if os_.get("proof_runner") != ns.get("proof_runner"):
        downgrade = True  # 判据的执行环境变了
    old_floor = {b["id"]: b.get("proof", PROOF_FLOOR_STRONG) for b in old["mission"]["behaviors"]}
    for b in new["mission"]["behaviors"]:
        if old_floor.get(b["id"]) == PROOF_FLOOR_STRONG and b.get("proof", PROOF_FLOOR_STRONG) != PROOF_FLOOR_STRONG:
            downgrade = True
    if downgrade:
        kinds.append(CHANGE_ASSURANCE_DOWNGRADE)

    return kinds or [CHANGE_NEUTRAL]


def contract_record(contract: dict[str, Any], plan_path: Path | None) -> dict[str, Any]:
    """写进 ledger 的 contract 段。"""
    return {
        "plan_path": str(plan_path) if plan_path else None,
        "plan_sha256": sha256_file(plan_path) if plan_path else None,
        "mission": contract["mission"],
        "authority": contract["authority"],
        "assurance": contract["assurance"],
        "digests": facet_digests(contract),
        "history": [],
    }
