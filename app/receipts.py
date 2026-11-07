"""'Receipts' check: every number in the final answer must trace back to sandbox output.

LLMs are good at sounding precise. This check makes the agent *prove* it: we
extract each number from the answer and look for a matching value (allowing
for rounding, thousands separators, k/M/B suffixes and percent conversions)
in what the code actually printed.
"""

from __future__ import annotations

import re

_NUM = re.compile(
    r"(?<![\w.])[-−]?\$?(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?\s?(%|k\b|K\b|M\b|B\b|bn\b|million\b|billion\b|thousand\b)?",
)
_SCALE = {"k": 1e3, "K": 1e3, "thousand": 1e3, "M": 1e6, "million": 1e6, "B": 1e9, "bn": 1e9, "billion": 1e9}
_PLAIN = re.compile(r"[-−]?\d[\d,]*(?:\.\d+)?(?:[eE][-+]?\d+)?")


def _stdout_values(stdout: str) -> list[float]:
    vals = []
    for m in _PLAIN.finditer(stdout):
        try:
            vals.append(float(m.group(0).replace(",", "").replace("−", "-")))
        except ValueError:
            continue
    return vals


def _claims(answer: str) -> list[tuple[str, float, float, bool]]:
    """(raw text, value, tolerance, is_percent) for every number worth checking."""
    out = []
    for m in _NUM.finditer(answer):
        whole, frac, suffix = m.group(1), m.group(2) or "", m.group(3) or ""
        raw = m.group(0).strip()
        value = float((whole.replace(",", "")) + frac)
        if raw.startswith(("-", "−")):
            value = -value
        decimals = len(frac) - 1 if frac else 0
        scale = _SCALE.get(suffix.strip(), 1.0)
        is_pct = suffix.strip() == "%"
        # Small bare integers ("top 3", "2 charts") and plain years are not claims about the data.
        if not frac and not suffix and abs(value) < 10:
            continue
        if not frac and not suffix and 1990 <= value <= 2100:
            continue
        tol = 0.5 * (10 ** -decimals) * scale
        out.append((raw, value * scale, max(tol, abs(value * scale) * 1e-6), is_pct))
    return out


def verify(answer: str, stdout: str) -> dict:
    values = _stdout_values(stdout)
    checked, matched, unmatched = 0, 0, []
    for raw, value, tol, is_pct in _claims(answer):
        checked += 1
        candidates = [(value, tol)]
        if is_pct:
            candidates.append((value / 100, tol / 100))  # 12.5% in the answer vs 0.125 in stdout
        hit = any(abs(abs(c) - abs(v)) <= t for c, t in candidates for v in values)
        if hit:
            matched += 1
        else:
            unmatched.append(raw)
    return {"checked": checked, "matched": matched, "unmatched": unmatched[:10],
            "score": round(matched / checked, 3) if checked else None}
