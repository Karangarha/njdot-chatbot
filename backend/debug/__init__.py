"""Debug package for NJDOT pipeline instrumentation.

Shared utilities: sys.path bootstrap, ANSI colour helpers, print primitives.
Import this package before any app.* imports so the backend root is on sys.path.
"""

from __future__ import annotations

import sys
from pathlib import Path

# ── Put backend/ on sys.path so "app.*" imports resolve ──────────────────────
_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

# ── ANSI colours ──────────────────────────────────────────────────────────────
_GREEN  = "\033[92m"
_RED    = "\033[91m"
_YELLOW = "\033[93m"
_CYAN   = "\033[96m"
_BOLD   = "\033[1m"
_DIM    = "\033[2m"
_RESET  = "\033[0m"


def green(t: str)  -> str: return f"{_GREEN}{t}{_RESET}"
def red(t: str)    -> str: return f"{_RED}{t}{_RESET}"
def yellow(t: str) -> str: return f"{_YELLOW}{t}{_RESET}"
def cyan(t: str)   -> str: return f"{_CYAN}{t}{_RESET}"
def bold(t: str)   -> str: return f"{_BOLD}{t}{_RESET}"
def dim(t: str)    -> str: return f"{_DIM}{t}{_RESET}"


# ── Print helpers ─────────────────────────────────────────────────────────────

def section(title: str) -> None:
    """Print a heavy double-line banner."""
    bar = "═" * 80
    inner = f"  {bold(title)}  "
    print(f"\n{bar}\n{inner}\n{bar}")


def subsection(title: str) -> None:
    """Print a lighter sub-section header."""
    trailer = "─" * max(0, 78 - len(title) - 4)
    print(f"\n{cyan('──')} {bold(title)} {cyan(trailer)}")


def hr() -> None:
    print("─" * 80)


def status_badge(status: str) -> str:
    s = status.upper()
    if s == "PASS":
        return green("✓ PASS")
    if s == "FAIL":
        return red("✗ FAIL")
    if s == "WARNING":
        return yellow("⚠ WARNING")
    return dim(s)
