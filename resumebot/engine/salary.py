"""Read the posted pay range from a job and compare it with your salary floor.

Postings mix pay ranges with other dollar amounts ("$500 home-office stipend", "$75 learning
budget"), so only plausible annual salaries (or hourly rates, annualized) are counted.
"""
from __future__ import annotations

import re

HOURS_PER_YEAR = 2080
_NUM = r"(?:[A-Z]{1,3})?\$?\s?(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s?([kK])?"  # "$", "CA$", "US$"
RANGE_RE = re.compile(_NUM + r"\s*(?:-|–|—|to)\s*" + _NUM + r"(?P<tail>[^\n]{0,40})")
SINGLE_RE = re.compile(r"(?:salary|base pay|base salary|compensation|pay range|ote)[^\n$]{0,40}\$\s?"
                       r"(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s?([kK])?", re.I)
HOURLY_RE = re.compile(r"(?:/|per)\s*(?:hour|hr)\b|hourly", re.I)


def _amount(number: str, k: str | None) -> float:
    value = float(number.replace(",", ""))
    return value * 1000 if k else value


def _annual(value: float, hourly: bool) -> float | None:
    if hourly and 10 <= value <= 400:
        return value * HOURS_PER_YEAR
    if 20_000 <= value <= 2_000_000:
        return value
    return None


def posted_max(*texts: str) -> float | None:
    """Highest annual pay found in any posted range; None if no salary is posted."""
    best: float | None = None
    for text in texts:
        if not text:
            continue
        for m in RANGE_RE.finditer(text):
            hourly = bool(HOURLY_RE.search(m.group("tail") or ""))
            for number, k in ((m.group(1), m.group(2)), (m.group(3), m.group(4))):
                value = _annual(_amount(number, k), hourly)
                if value is not None:
                    best = max(best or 0, value)
        if best is None:
            for m in SINGLE_RE.finditer(text):
                value = _annual(_amount(m.group(1), m.group(2)), False)
                if value is not None:
                    best = max(best or 0, value)
    return best


def below_floor(salary: str, description: str, floor: int | None) -> tuple[bool, float | None]:
    """True when the job posts pay and the top of the range is under your floor."""
    if not floor:
        return False, None
    top = posted_max(salary, description)
    return (top is not None and top < floor), top
