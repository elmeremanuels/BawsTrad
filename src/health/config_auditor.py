from __future__ import annotations

"""
Config Auditor — System 5 (PROBLEEM 5).

Weekly scan of src/**/*.py for hardcoded magic values that should live in
config.yaml. Reports findings to Discord; raises zero exceptions so it
never disturbs the live bot.

Hardcoded values we hunt:
  - Time strings  e.g. "09:30", '15:55'
  - Large numbers  e.g. 20_000_000, 25000, 300
  - Magic floats   e.g. 0.01, 2.0, 5.0  (outside obvious test/comment context)

False-positive reduction:
  - Lines that are comments (#) are skipped
  - Values already present verbatim in config.yaml are not reported
  - Docstring lines are skipped
  - Common non-magic literals (0, 1, 2, 100) are excluded
  - Import / __version__ lines are excluded
  - Test files (tests/, conftest.py) are excluded
"""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_SRC_DIR      = _PROJECT_ROOT / "src"
_CONFIG_FILE  = _PROJECT_ROOT / "config.yaml"

# ─── Pattern definitions ──────────────────────────────────────────────────────

# Time strings: "09:30", '15:55', "04:00"
_PATTERN_TIME = re.compile(r"""["'](\d{1,2}:\d{2})["']""")

# Large integers: anything >= 1000 written as a bare literal
# Excludes: trailing `_` separator numbers captured as whole thing
_PATTERN_LARGE_INT = re.compile(r"""\b([1-9]\d{3,})\b""")

# Magic floats: decimal numbers that are likely thresholds / percentages
# Excludes: version strings, import lines
_PATTERN_MAGIC_FLOAT = re.compile(r"""(?<![_\w])(\d+\.\d+)(?![_\w])""")

# Common non-magic literals to skip (too many false positives)
_SKIP_FLOATS  = {"0.0", "1.0", "0.5", "0.25", "0.75", "100.0", "0.1", "0.2"}
_SKIP_INTS    = {"1000", "1001", "2000"}  # common HTTP codes, loop bounds

# Lines we skip entirely
_SKIP_LINE_PATTERNS = [
    re.compile(r"^\s*#"),              # comment lines
    re.compile(r"^\s*\"\"\""),          # start of docstring
    re.compile(r"^\s*'''"),
    re.compile(r"^\s*(import|from)\s"), # import lines
    re.compile(r"^\s*__version__"),     # version assignments
    re.compile(r"NOAUDIT"),             # explicit opt-out marker
]


@dataclass
class AuditFinding:
    file:    str
    line_no: int
    value:   str
    kind:    str        # "time_value" | "large_number" | "magic_float"
    context: str        # surrounding line content (truncated)


def _load_config_text() -> str:
    try:
        return _CONFIG_FILE.read_text()
    except Exception:
        return ""


def _skip_line(line: str) -> bool:
    return any(p.search(line) for p in _SKIP_LINE_PATTERNS)


def _scan_file(path: Path, config_text: str) -> List[AuditFinding]:
    findings: List[AuditFinding] = []
    try:
        lines = path.read_text(errors="replace").splitlines()
    except Exception as exc:
        logger.warning("config_auditor: could not read %s: %s", path, exc)
        return []

    in_docstring = False

    for lineno, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()

        # Track docstring blocks
        if '"""' in line or "'''" in line:
            count = line.count('"""') + line.count("'''")
            if count % 2 != 0:
                in_docstring = not in_docstring
        if in_docstring:
            continue

        if _skip_line(line):
            continue

        context = raw_line[:120]

        # ── Time strings ──────────────────────────────────────────────────────
        for m in _PATTERN_TIME.finditer(raw_line):
            val = m.group(1)
            if val not in config_text:
                findings.append(AuditFinding(
                    file=str(path.relative_to(_PROJECT_ROOT)),
                    line_no=lineno, value=val,
                    kind="time_value", context=context,
                ))

        # ── Large integers ────────────────────────────────────────────────────
        for m in _PATTERN_LARGE_INT.finditer(raw_line):
            val = m.group(1)
            if val in _SKIP_INTS:
                continue
            # Skip if it looks like a year (2020-2030)
            if re.match(r"^20[12]\d$", val):
                continue
            # Skip if value appears verbatim in config.yaml
            if val in config_text:
                continue
            findings.append(AuditFinding(
                file=str(path.relative_to(_PROJECT_ROOT)),
                line_no=lineno, value=val,
                kind="large_number", context=context,
            ))

        # ── Magic floats ──────────────────────────────────────────────────────
        for m in _PATTERN_MAGIC_FLOAT.finditer(raw_line):
            val = m.group(1)
            if val in _SKIP_FLOATS:
                continue
            # Skip very common small floats that are clearly not thresholds
            try:
                fval = float(val)
            except ValueError:
                continue
            if fval in (0.0, 1.0, 0.5, 0.25, 0.75, 100.0, 0.1, 0.2):
                continue
            if val in config_text:
                continue
            findings.append(AuditFinding(
                file=str(path.relative_to(_PROJECT_ROOT)),
                line_no=lineno, value=val,
                kind="magic_float", context=context,
            ))

    return findings


class ConfigAuditor:
    """
    Scans src/**/*.py for hardcoded magic values not present in config.yaml.

    Usage:
        findings = ConfigAuditor().audit()
        # findings is a list of AuditFinding objects
        # also posts to Discord automatically if findings exist
    """

    def __init__(self, post_to_discord: bool = True) -> None:
        self.post_to_discord = post_to_discord

    def audit(self) -> List[AuditFinding]:
        config_text = _load_config_text()
        all_findings: List[AuditFinding] = []

        # Scan all .py files except test files and this file itself
        for py_file in sorted(_SRC_DIR.rglob("*.py")):
            # Skip test files
            if any(part in {"tests", "test"} for part in py_file.parts):
                continue
            if py_file.name.startswith("test_"):
                continue
            # Skip self
            if py_file.name == "config_auditor.py":
                continue

            file_findings = _scan_file(py_file, config_text)
            all_findings.extend(file_findings)

        logger.info("config_auditor: scan complete, %d findings", len(all_findings))

        if all_findings and self.post_to_discord:
            self._discord_report(all_findings)

        return all_findings

    @staticmethod
    def _discord_report(findings: List[AuditFinding]) -> None:
        """Post a compact finding summary to Discord."""
        try:
            import asyncio
            from src.alerts.discord import post_message
            from src.config import settings
            from collections import Counter

            # Group by kind
            by_kind = Counter(f.kind for f in findings)
            by_file = Counter(f.file for f in findings)
            top_files = by_file.most_common(3)

            lines = [
                f"⚠️ **Config Audit** — {len(findings)} hardcoded values found",
                f"   time_value={by_kind['time_value']}  "
                f"large_number={by_kind['large_number']}  "
                f"magic_float={by_kind['magic_float']}",
                "   Top files:",
            ]
            for fname, count in top_files:
                lines.append(f"   • `{fname}` — {count} values")
            lines.append("Run `python -m src.health.config_auditor` for full report.")

            asyncio.run(post_message(settings.DISCORD_WEBHOOK_URL, "\n".join(lines)))
        except Exception as exc:
            logger.warning("config_auditor: Discord alert failed: %s", exc)

    def print_report(self, findings: Optional[List[AuditFinding]] = None) -> None:
        """Print a full human-readable report to stdout."""
        if findings is None:
            findings = self.audit()

        if not findings:
            print("✅ Config audit: no hardcoded magic values found.")
            return

        print(f"\n{'─'*70}")
        print(f"CONFIG AUDIT — {len(findings)} findings")
        print(f"{'─'*70}")

        current_file = None
        for f in sorted(findings, key=lambda x: (x.file, x.line_no)):
            if f.file != current_file:
                print(f"\n  📄 {f.file}")
                current_file = f.file
            print(f"    L{f.line_no:>4}  [{f.kind:>14}]  {f.value!r:<18}  {f.context.strip()[:60]}")

        print(f"\n{'─'*70}")
        print(
            "To suppress: add  # NOAUDIT  on the line, "
            "or add the value to config.yaml.\n"
        )


if __name__ == "__main__":
    auditor = ConfigAuditor(post_to_discord=False)
    auditor.print_report()
