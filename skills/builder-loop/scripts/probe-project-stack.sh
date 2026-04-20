#!/usr/bin/env bash
# probe-project-stack.sh — 探测项目语言栈/测试框架/lint工具/目录布局
#
# 用法：bash probe-project-stack.sh [project_root]
#   project_root 默认为当前目录
#
# 输出（stdout）：JSON 结构
#   {
#     "language": "python|node|go|rust|unknown",
#     "test_framework": "pytest|jest|gotest|cargo|unknown",
#     "lint_tools": ["ruff", "mypy", ...],
#     "source_dirs": ["src", "lib"],
#     "test_dirs": ["tests", "spec"],
#     "recommended_pass_cmd": [{"stage":..., "cmd":..., "timeout":...}, ...]
#   }
#
# 退出码：始终 0（探测不出来就返回 unknown，不算错误）

set -uo pipefail

ROOT="${1:-$(pwd)}"
cd "$ROOT" || { echo '{"error":"cannot cd"}' ; exit 0; }

# ---- helpers ----
has_file() { [ -f "$1" ]; }
has_dir()  { [ -d "$1" ]; }
# shellcheck disable=SC2012,SC2086
# $1 is intentionally an unquoted glob pattern
glob_first() { ls -1 $1 2>/dev/null | head -1; }

# ---- 语言探测 ----
LANG="unknown"
if has_file pyproject.toml || has_file setup.py || has_file requirements.txt; then
  LANG="python"
elif has_file package.json; then
  LANG="node"
elif has_file go.mod; then
  LANG="go"
elif has_file Cargo.toml; then
  LANG="rust"
fi

# ---- 测试框架 + lint 工具探测 ----
TEST_FW="unknown"
LINT_TOOLS=()

case "$LANG" in
  python)
    if has_file pyproject.toml && grep -q '\[tool.pytest' pyproject.toml 2>/dev/null; then TEST_FW="pytest"
    elif has_file pytest.ini || has_file conftest.py || [ -n "$(glob_first 'tests/conftest.py')" ]; then TEST_FW="pytest"
    fi
    has_file ruff.toml || (has_file pyproject.toml && grep -q '\[tool.ruff' pyproject.toml 2>/dev/null) && LINT_TOOLS+=("ruff")
    has_file mypy.ini || (has_file pyproject.toml && grep -q '\[tool.mypy' pyproject.toml 2>/dev/null) && LINT_TOOLS+=("mypy")
    ;;
  node)
    if has_file package.json; then
      grep -q '"jest"' package.json 2>/dev/null && TEST_FW="jest"
      grep -q '"vitest"' package.json 2>/dev/null && TEST_FW="vitest"
      grep -q '"eslint"' package.json 2>/dev/null && LINT_TOOLS+=("eslint")
      grep -q '"typescript"' package.json 2>/dev/null && LINT_TOOLS+=("tsc")
    fi
    ;;
  go)
    TEST_FW="gotest"
    command -v golangci-lint >/dev/null 2>&1 && LINT_TOOLS+=("golangci-lint")
    ;;
  rust)
    TEST_FW="cargo"
    LINT_TOOLS+=("cargo-clippy")
    ;;
esac

# ---- 目录布局探测 ----
SOURCE_DIRS=()
for d in src lib app pkg internal cmd; do has_dir "$d" && SOURCE_DIRS+=("$d"); done
TEST_DIRS=()
for d in tests test spec __tests__ t; do has_dir "$d" && TEST_DIRS+=("$d"); done

# ---- pytest 命令验证（自动排除问题插件）----
# 用 --co (collect-only) 快速验证 pytest 能否启动，不实际执行测试
# 常见问题：pytest-html 依赖废弃的 py.xml 模块导致启动崩溃
KNOWN_BAD_PYTEST_PLUGINS="html sugar"

validate_pytest() {
  local base="pytest -x"
  # pytest 不在 PATH 则跳过验证
  command -v pytest >/dev/null 2>&1 || { echo "$base"; return; }
  # 快速验证：--co 会加载全部插件，能暴露插件兼容性问题
  if timeout 10s pytest --co -q >/dev/null 2>&1; then
    echo "$base"
    return
  fi
  # 逐个累加排除已知问题插件
  local flags=""
  for p in $KNOWN_BAD_PYTEST_PLUGINS; do
    flags="$flags -p no:$p"
    if timeout 10s pytest --co -q $flags >/dev/null 2>&1; then
      echo "[probe] auto-excluded pytest plugins:$flags" >&2
      echo "pytest -x$flags"
      return
    fi
  done
  # 全部排除仍失败，返回基础命令（交给 smoke test 暴露）
  echo "$base"
}

# ---- 推荐 pass_cmd ----
build_recommended() {
  local items=()
  for tool in "${LINT_TOOLS[@]}"; do
    case "$tool" in
      ruff)            items+=('{"stage":"lint","cmd":"ruff check .","timeout":60}') ;;
      mypy)            items+=('{"stage":"typecheck","cmd":"mypy .","timeout":120}') ;;
      eslint)          items+=('{"stage":"lint","cmd":"npx eslint .","timeout":120}') ;;
      tsc)             items+=('{"stage":"typecheck","cmd":"npx tsc --noEmit","timeout":120}') ;;
      golangci-lint)   items+=('{"stage":"lint","cmd":"golangci-lint run","timeout":120}') ;;
      cargo-clippy)    items+=('{"stage":"lint","cmd":"cargo clippy -- -D warnings","timeout":180}') ;;
    esac
  done
  case "$TEST_FW" in
    pytest)  local pcmd; pcmd="$(validate_pytest)"
             local item; item="$(PCMD="$pcmd" python3 -c "import os,json; print(json.dumps({'stage':'test','cmd':os.environ['PCMD'],'timeout':300}))")"
             items+=("$item") ;;
    jest)    items+=('{"stage":"test","cmd":"npx jest","timeout":300}') ;;
    vitest)  items+=('{"stage":"test","cmd":"npx vitest run","timeout":300}') ;;
    gotest)  items+=('{"stage":"test","cmd":"go test ./...","timeout":300}') ;;
    cargo)   items+=('{"stage":"test","cmd":"cargo test","timeout":600}') ;;
  esac
  ( IFS=, ; echo "[${items[*]}]" )
}

# ---- 输出 JSON（用 python 安全 dump，避免手写转义出错）----
LANG="$LANG" TEST_FW="$TEST_FW" \
LINT_TOOLS_CSV="$(IFS=,; echo "${LINT_TOOLS[*]:-}")" \
SOURCE_DIRS_CSV="$(IFS=,; echo "${SOURCE_DIRS[*]:-}")" \
TEST_DIRS_CSV="$(IFS=,; echo "${TEST_DIRS[*]:-}")" \
RECOMMENDED_RAW="$(build_recommended)" \
python3 - <<'PY'
import json, os
def csv_to_list(s):
    return [x for x in s.split(',') if x] if s else []
out = {
    "language":      os.environ['LANG'],
    "test_framework": os.environ['TEST_FW'],
    "lint_tools":    csv_to_list(os.environ['LINT_TOOLS_CSV']),
    "source_dirs":   csv_to_list(os.environ['SOURCE_DIRS_CSV']),
    "test_dirs":     csv_to_list(os.environ['TEST_DIRS_CSV']),
    "recommended_pass_cmd": json.loads(os.environ['RECOMMENDED_RAW']),
}
print(json.dumps(out, indent=2, ensure_ascii=False))
PY
