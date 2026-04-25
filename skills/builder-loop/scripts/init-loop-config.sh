#!/usr/bin/env bash
# init-loop-config.sh — 底层 loop.yml 接入脚本（纯 bash，可独立调用）
#
# 用法：
#   bash init-loop-config.sh <project_root> --config <choice_json_file>
#   bash init-loop-config.sh <project_root>            # 用 stdin 读 JSON
#
# 输入 JSON 结构（用户向导阶段拼出来的）：
#   {
#     "pass_cmd": [{"stage": "test", "cmd": "pytest -x", "timeout": 300}, ...],
#     "max_iterations": 5,
#     "layout": {"source_dirs": ["src"], "test_dirs": ["tests"]},
#     "task_description": "由 init 向导生成于 ..."
#   }
#
# 行为：
#   1. 校验项目根 + JSON 合法
#   2. 写 <project_root>/.claude/loop.yml（如已存在则备份为 .bak.timestamp）
#   3. 追加 <project_root>/.gitignore 两行（已有则跳过）
#   4. mkdir <project_root>/.claude/loop-runs/
#   5. 输出 OK + loop.yml 路径 / 失败时输出 FAIL + 原因到 stderr
#
# 退出码：0=成功 / 1=参数错 / 2=JSON 非法 / 3=写入失败

set -euo pipefail

PROJECT_ROOT="${1:?project_root required}"
shift

CONFIG_FILE=""
if [ "${1:-}" = "--config" ]; then
  CONFIG_FILE="${2:?--config requires file path}"
fi

# ---- 读 JSON ----
if [ -n "$CONFIG_FILE" ]; then
  [ -f "$CONFIG_FILE" ] || { echo "[init-loop] config file not found: $CONFIG_FILE" >&2; exit 1; }
  JSON="$(cat "$CONFIG_FILE")"
else
  JSON="$(cat)"
fi

# ---- 校验 ----
[ -d "$PROJECT_ROOT" ] || { echo "[init-loop] project_root not a dir: $PROJECT_ROOT" >&2; exit 1; }
echo "$JSON" | python3 -c "import sys, json; json.load(sys.stdin)" 2>/dev/null \
  || { echo "[init-loop] invalid JSON input" >&2; exit 2; }

LOOP_DIR="${PROJECT_ROOT}/.claude"
LOOP_YML="${LOOP_DIR}/loop.yml"
LOG_DIR="${LOOP_DIR}/loop-runs"
GITIGNORE="${PROJECT_ROOT}/.gitignore"

mkdir -p "$LOOP_DIR" "$LOG_DIR"

# ---- 已有 loop.yml 则备份 ----
if [ -f "$LOOP_YML" ]; then
  BAK="${LOOP_YML}.bak.$(date +%s)"
  cp "$LOOP_YML" "$BAK"
  echo "[init-loop] 已备份现有 loop.yml → $BAK" >&2
fi

# ---- 用 python 把 JSON 转成 yaml 写入 ----
# 用 json.dumps 自动处理所有特殊字符转义（\n \t " \\ 等），不手写 replace
JSON_INPUT="$JSON" LOOP_YML="$LOOP_YML" python3 - <<'PY'
import os, json

cfg = json.loads(os.environ['JSON_INPUT'])
yml = os.environ['LOOP_YML']

def yaml_str(s):
    """用 json.dumps 输出，保证特殊字符（\n \t " \\ 等）都正确转义"""
    return json.dumps(s, ensure_ascii=False)

lines = []
lines.append("# 由 builder-loop init 向导生成；可手动编辑")
desc = cfg.get('task_description', '')
if desc:
    # 多行描述用 # 注释每行
    for line in desc.split('\n'):
        lines.append(f"# {line}")
lines.append("")
lines.append("pass_cmd:")
for item in cfg.get('pass_cmd', []):
    s = yaml_str(item.get('stage',''))
    c = yaml_str(item.get('cmd',''))
    t = item.get('timeout', 300)
    lines.append(f'  - {{ stage: {s}, cmd: {c}, timeout: {t} }}')
lines.append("")
lines.append(f"max_iterations: {cfg.get('max_iterations', 5)}")
lines.append("")
layout = cfg.get('layout', {})
if layout.get('source_dirs') or layout.get('test_dirs'):
    lines.append("layout:")
    if layout.get('source_dirs'):
        lines.append(f"  source_dirs: {json.dumps(layout['source_dirs'])}")
    if layout.get('test_dirs'):
        lines.append(f"  test_dirs: {json.dumps(layout['test_dirs'])}")

# worktree 隔离（T2.3：向导可选；缺省不写 = schema 默认 enabled=false）
wt = cfg.get('worktree') or {}
if wt:
    lines.append("")
    lines.append("worktree:")
    lines.append(f"  enabled: {str(bool(wt.get('enabled', False))).lower()}")
    if wt.get('base_dir'):
        lines.append(f"  base_dir: {yaml_str(wt['base_dir'])}")
    if wt.get('branch_prefix'):
        lines.append(f"  branch_prefix: {yaml_str(wt['branch_prefix'])}")

# ---- V1.9 judge agent 提示（默认启用，仅注释形式列出可调参数）----
lines.append("")
lines.append("# ---- V1.9 judge agent ----")
lines.append("# 默认启用，无需配置（凭证缺失会自动降级回 PASS_CMD 二值判据）。")
lines.append("# 如需自定义，取消下列任一字段注释：")
lines.append("# judge:")
lines.append("#   enabled: true                  # false 完全关闭 judge")
lines.append("#   model: \"\"                      # 留空走 env / 默认 fallback (claude-haiku-4-5)")
lines.append("#   confidence_threshold: 0.5      # 置信度低于此值降级")
lines.append("#   max_consecutive_nudges: 2      # 连续 nudge 上限（防 LLM 判据脱缰）")
lines.append("#   api_timeout_sec: 8")
lines.append("# 详见 ~/.claude/skills/builder-loop/docs/judge-agent.md")

with open(yml, 'w') as f:
    f.write('\n'.join(lines) + '\n')
PY

# ---- 追加 .gitignore（已有则跳过）----
add_gitignore() {
  local pattern="$1"
  if [ ! -f "$GITIGNORE" ] || ! grep -qFx "$pattern" "$GITIGNORE"; then
    echo "$pattern" >> "$GITIGNORE"
    echo "[init-loop] .gitignore 已加：$pattern" >&2
  fi
}
add_gitignore ".claude/builder-loop/"
add_gitignore ".claude/loop-runs/"

# T2.8：worktree.enabled=true 时把 base_dir 加进 .gitignore（向导联动）
WT_BASE_DIR="$(echo "$JSON" | python3 -c "
import sys, json
d = json.load(sys.stdin)
wt = d.get('worktree') or {}
if wt.get('enabled'):
    bd = wt.get('base_dir') or '.claude/worktrees'
    print(bd.rstrip('/') + '/')
" 2>/dev/null || echo "")"
if [ -n "$WT_BASE_DIR" ]; then
  add_gitignore "$WT_BASE_DIR"
fi

# Bug fix: .claude/* 通配符可能拦住 loop.yml，加例外确保能 git track
if git -C "$PROJECT_ROOT" check-ignore -q ".claude/loop.yml" 2>/dev/null; then
  add_gitignore "!.claude/loop.yml"
  echo "[init-loop] ⚠️  检测到 .gitignore 会拦 loop.yml，已加 !.claude/loop.yml 例外" >&2
fi

echo "OK $LOOP_YML"
