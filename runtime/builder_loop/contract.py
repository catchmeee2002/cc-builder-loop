"""contract：plan.md 里 `<!-- builder-loop-contract -->` 标签内的 JSON。

三个事实面各自算 canonical digest：mission（语义）/ authority（写边界）/ assurance（判据）。
mission 变 → revision+1 且需用户确认；authority 扩大、assurance 降级需用户授权。
`assurance.machine_commands` 不由 planner 写，start 时从 loop.yml 冻结进来。
"""

from __future__ import annotations

import difflib
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

# contract 字段的唯一来源：validate_contract 的允许键、`bl contract fields` 的输出、
# SKILL 与文档的指路都从这张表派生（原则二）。readers 是 brief 实际渲染该字段的角色——
# 只有含 tester 的字段 tester 才看得到，需要它守住的约束必须写在这些字段里。
# source=frozen 表示由 start 从 loop.yml 冻结，planner 不写。
FIELD_TABLE: tuple[dict[str, Any], ...] = (
    {"path": "schema", "type": "string", "readers": ["runtime"], "source": "planner",
     "description": f"固定为 {CONTRACT_SCHEMA}"},
    {"path": "mission", "type": "object", "readers": ["runtime"], "source": "planner",
     "description": "语义面：目标、行为、接口、约束"},
    {"path": "mission.revision", "type": "integer", "readers": ["tester", "reviewer"], "source": "planner",
     "description": "mission 修订号；改 mission 面时 +1 且需用户确认"},
    {"path": "mission.slug", "type": "string", "readers": ["runtime"], "source": "planner",
     "description": "小写字母/数字/连字符，2–49 字符"},
    {"path": "mission.objective", "type": "string", "readers": ["tester", "reviewer"], "source": "planner",
     "description": "一句话目标"},
    {"path": "mission.behaviors", "type": "array<object>", "readers": ["tester", "reviewer"], "source": "planner",
     "description": "非空；每条是可盲测的 given / when / then"},
    {"path": "mission.behaviors[].id", "type": "string", "readers": ["tester", "reviewer"], "source": "planner",
     "description": "behavior 标识，run 内唯一"},
    {"path": "mission.behaviors[].given", "type": "string", "readers": ["tester", "reviewer"], "source": "planner",
     "description": "前置条件，写具体值域"},
    {"path": "mission.behaviors[].when", "type": "string", "readers": ["tester", "reviewer"], "source": "planner",
     "description": "触发的调用或操作"},
    {"path": "mission.behaviors[].then", "type": "string", "readers": ["tester", "reviewer"], "source": "planner",
     "description": "可观察结果，必须可机判"},
    {"path": "mission.behaviors[].boundaries", "type": "array<string>", "readers": ["tester", "reviewer"], "source": "planner",
     "description": "边界条件；tester 盲写测试的厚度来源，tester 需要守住的约束写在这里"},
    {"path": "mission.behaviors[].invariants", "type": "array<string>", "readers": ["tester", "reviewer"], "source": "planner",
     "description": "不应被破坏的既有行为；tester 需要守住的约束写在这里"},
    {"path": "mission.behaviors[].proof", "type": "string", "readers": ["tester"], "source": "planner",
     "description": "proof 下限：strong（缺省）或 reviewed-boundaries"},
    {"path": "mission.interfaces", "type": "array<string>", "readers": ["tester", "reviewer"], "source": "planner",
     "description": "tester 据此 import 的唯一接口来源，含被测行为依赖的既有接口"},
    {"path": "mission.acceptance_cases", "type": "array", "readers": [], "source": "planner",
     "description": "blackbox 留位，首版不消费"},
    {"path": "mission.trust_boundaries", "type": "array<string>", "readers": ["tester", "reviewer"], "source": "planner",
     "description": "不能碰什么、必须兼容什么"},
    {"path": "mission.mock_strategy", "type": "object", "readers": ["tester", "reviewer"], "source": "planner",
     "description": "依赖名 → mock 方式"},
    {"path": "authority", "type": "object", "readers": ["runtime"], "source": "planner",
     "description": "写边界面"},
    {"path": "authority.target_branch", "type": "string|null", "readers": ["runtime"], "source": "planner",
     "description": "省略或 null = start 时的当前分支"},
    {"path": "authority.builder_write", "type": "array<string>", "readers": ["runtime"], "source": "planner",
     "description": "非空；builder 可写的路径 glob"},
    {"path": "authority.tester_write", "type": "array<string>", "readers": ["tester", "runtime"], "source": "planner",
     "description": "tester 可写的路径 glob；required 含 tester 时不能为空"},
    {"path": "authority.protected_paths", "type": "array<string>", "readers": ["tester", "runtime"], "source": "planner",
     "description": "谁都不能动的路径"},
    {"path": "authority.control_basenames", "type": "array<string>", "readers": ["runtime"], "source": "frozen",
     "description": "控制面文件名规则；builder 靠 glob 顺带命中不算授权"},
    {"path": "assurance", "type": "object", "readers": ["runtime"], "source": "planner",
     "description": "判据面"},
    {"path": "assurance.required", "type": "array<string>", "readers": ["runtime"], "source": "planner",
     "description": "必须包含 machine；proof 依赖 tester"},
    {"path": "assurance.proof_kinds", "type": "array<string>", "readers": ["runtime"], "source": "planner",
     "description": "baseline-red / mutation / reviewed-boundaries 的子集"},
    {"path": "assurance.review_focus", "type": "array<string>", "readers": ["reviewer"], "source": "planner",
     "description": "只给 reviewer 的审查重点；tester 看不到此字段，需要 tester 守住的约束不要写在这里"},
    {"path": "assurance.machine_stages", "type": "array<string>", "readers": ["runtime"], "source": "planner",
     "description": "方案依赖的 loop.yml stage 名（只写名字）"},
    {"path": "assurance.machine_commands", "type": "array", "readers": ["runtime"], "source": "frozen",
     "description": "由 start 从 loop.yml 冻结"},
    {"path": "assurance.max_iterations", "type": "integer", "readers": ["runtime"], "source": "frozen",
     "description": "由 start 从 loop.yml 冻结"},
    {"path": "assurance.proof_runner", "type": "object", "readers": ["tester", "reviewer", "runtime"], "source": "frozen",
     "description": "由 start 从 loop.yml 冻结：framework / cmd / link_paths"},
    {"path": "assurance.evidence_neutral_paths", "type": "array<string>", "readers": ["runtime"], "source": "frozen",
     "description": "对判据中性的路径 glob；由 start 从 loop.yml 冻结"},
)


def field_table() -> list[dict[str, Any]]:
    return [dict(f, readers=list(f["readers"])) for f in FIELD_TABLE]


OWNER_BUILDER = "builder"
OWNER_TESTER = "tester"

CHANGE_MISSION = "MISSION_REVISION"
CHANGE_AUTHORITY_EXPAND = "AUTHORITY_EXPAND"
CHANGE_ASSURANCE_DOWNGRADE = "ASSURANCE_DOWNGRADE"
CHANGE_NEUTRAL = "NEUTRAL"


# ---------------------------------------------------------------- 标签提取


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def extract_tag(text: str, name: str) -> str | None:
    """`<!-- name -->` … `<!-- /name -->` 之间的内容；恰好一对且开在前才合法。

    标签形态先在这里判定完再取内容：配对不合法时不进入调用方的 JSON 解析，
    否则孤儿开标签会让解析区间横跨正文，报出的错指向 contract JSON 而非标签本身。
    """
    open_pat = re.compile(rf"<!--\s*{re.escape(name)}\s*-->")
    close_pat = re.compile(rf"<!--\s*/{re.escape(name)}\s*-->")
    opens = list(open_pat.finditer(text))
    closes = list(close_pat.finditer(text))
    if not opens and not closes:
        return None
    if len(opens) == len(closes) == 1 and opens[0].start() < closes[0].start():
        return text[opens[0].end():closes[0].start()]
    open_lines = [_line_of(text, m.start()) for m in opens]
    close_lines = [_line_of(text, m.start()) for m in closes]
    alternating = (
        len(opens) == len(closes) >= 2
        and all(o.start() < c.start() for o, c in zip(opens, closes))
        and all(c.start() < o.start() for c, o in zip(closes, opens[1:]))
    )
    if alternating:
        raise fatal("CONTRACT_DUPLICATE", f"plan 中出现多处 <!-- {name} --> 标签", count=len(opens))
    raise fatal(
        "CONTRACT_TAG_UNBALANCED",
        f"<!-- {name} --> 标签不配对：开标签在 1 起始的第 {open_lines} 行，闭标签在第 {close_lines} 行",
        open_lines=open_lines,
        close_lines=close_lines,
    )


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


def _children(prefix: str) -> dict[str, str]:
    """prefix 下一层的合法键 → 该键的完整点路径（behavior 带实际下标）。"""
    template = re.sub(r"\[\d+\]", "[]", prefix)
    out: dict[str, str] = {}
    for f in FIELD_TABLE:
        p = f["path"]
        if template:
            if not p.startswith(template + "."):
                continue
            rest = p[len(template) + 1:]
        else:
            rest = p
        head = rest.split(".", 1)[0]
        key = head[:-2] if head.endswith("[]") else head
        out[key] = (prefix + "." + key) if prefix else key
    return out


def _check_unknown_keys(obj: dict[str, Any], prefix: str) -> None:
    children = _children(prefix)
    for k in obj:
        if k in children:
            continue
        near = difflib.get_close_matches(k, list(children), n=1, cutoff=0.6)
        suggestion = children[near[0]] if near else None
        path = (prefix + "." + k) if prefix else k
        message = f"contract 里出现未知字段 {path}"
        if suggestion:
            message += f"；是否想写 {suggestion}？"
        raise fatal("CONTRACT_INVALID", message, path=path, suggestion=suggestion)


def validate_contract(contract: dict[str, Any]) -> None:
    if not isinstance(contract, dict):
        raise fatal("CONTRACT_INVALID", "contract 必须是 JSON 对象")
    if contract.get("schema") != CONTRACT_SCHEMA:
        raise fatal("CONTRACT_INVALID", f"schema 必须是 {CONTRACT_SCHEMA}", got=contract.get("schema"))
    for facet in FACETS:
        _require(contract, facet, dict, "contract")

    # 字段表之外的键在入口就拒（原则四）：拼错的字段不会静默丢失
    _check_unknown_keys(contract, "")
    for facet in FACETS:
        _check_unknown_keys(contract[facet], facet)
    # behaviors 的类型由后面的既有校验负责（错误码与消息保持原样）；这里只扫字典元素的键
    behaviors_raw = contract["mission"].get("behaviors")
    if isinstance(behaviors_raw, list):
        for i, b in enumerate(behaviors_raw):
            if isinstance(b, dict):
                _check_unknown_keys(b, f"mission.behaviors[{i}]")

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
    if "machine_stages" in s:
        ms = s["machine_stages"]
        if not isinstance(ms, list) or not ms or not all(isinstance(x, str) and x for x in ms) or len(set(ms)) != len(ms):
            raise fatal("CONTRACT_INVALID", "assurance.machine_stages 应为非空、不重复的 stage 名数组（只写名字，命令在 loop.yml）")
    if "machine_commands" in s and not isinstance(s["machine_commands"], list):
        raise fatal("CONTRACT_INVALID", "assurance.machine_commands 应为数组（通常由 start 冻结）")
    if "evidence_neutral_paths" in s:
        enp = s["evidence_neutral_paths"]
        if not isinstance(enp, list) or not all(isinstance(x, str) and x.strip() for x in enp):
            raise fatal("CONTRACT_INVALID", "assurance.evidence_neutral_paths 应为字符串数组（由 start 从 .claude/loop.yml 冻结，planner 不写）")


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


def check_machine_stages(contract: dict[str, Any], loop_config: LoopConfig) -> None:
    """方案声明依赖的 machine stage 必须都在 loop.yml 里（原则四：哪些 stage 当门禁是验收强度，写进 contract）。
    start / revise / validate --check-repo 都走这一处，不在调用点各写一份。"""
    need = contract["assurance"].get("machine_stages") or []
    have = [s.stage for s in loop_config.pass_cmd]
    missing = [n for n in need if n not in have]
    if missing:
        raise fatal("MACHINE_STAGE_MISSING", "contract 声明依赖的 machine stage 不在 .claude/loop.yml 的 pass_cmd 里（loop.yml 未纳入版本控制，可能被别的 session 改过）", missing=missing, available=have)


def freeze_assurance(contract: dict[str, Any], loop_config: LoopConfig) -> dict[str, Any]:
    check_machine_stages(contract, loop_config)
    frozen = freeze_authority(contract)
    frozen["assurance"]["machine_commands"] = [s.to_json() for s in loop_config.pass_cmd]
    frozen["assurance"]["max_iterations"] = loop_config.max_iterations
    frozen["assurance"]["proof_runner"] = dict(loop_config.proof_runner)
    # 与 machine_commands / proof_runner 同构：项目事实由 loop.yml 声明、start 冻结进 assurance 面计入 digest，
    # 改它需要一次被记录的 contract revise。planner 写了也以 loop.yml 为准（原则二：只有一个来源）。
    frozen["assurance"]["evidence_neutral_paths"] = list(loop_config.evidence_neutral_paths)
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
