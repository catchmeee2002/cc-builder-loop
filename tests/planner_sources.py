"""Read the actual Planner instruction surfaces for cross-surface contract audits."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def builder_planner_instructions(root: Path = ROOT) -> str:
    paths = ["skills/planner/SKILL.md"] + [
        f"skills/planner/references/{name}.md"
        for name in ("builder-contract", "builder-lifecycle", "builder-environment")
    ]
    return "\n\n".join((root / path).read_text() for path in paths)
