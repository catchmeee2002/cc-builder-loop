#!/usr/bin/env bash
set -euo pipefail

# Extract e2e test cases from plan file.
# Input: plan file path (arg 1)
# Output: stdout — markdown list between <!-- e2e-cases --> tags
# Exit: 0 = extracted, 1 = no tags or file missing

PLAN_FILE="${1:-}"

if [ -z "${PLAN_FILE}" ] || [ ! -f "${PLAN_FILE}" ]; then
    exit 1
fi

# V5.4: skip e2e-cases tags inside markdown fenced code blocks (``` ... ```)
CONTENT=$(awk '
  /^[[:space:]]*```/ { if (!capture) fence = !fence; next }
  fence { next }
  /^[[:space:]]*<!-- e2e-cases -->/ { capture = 1; next }
  /^[[:space:]]*<!-- \/e2e-cases -->/ { capture = 0; next }
  capture { print }
' "${PLAN_FILE}" | tr -d '\r')

if [ -z "${CONTENT}" ]; then
    exit 1
fi

printf '%s\n' "${CONTENT}"
