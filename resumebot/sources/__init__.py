from __future__ import annotations

from .ats_boards import Ashby, Greenhouse, Lever
from .base import Source
from .indeed import Indeed
from .linkedin import LinkedIn
from .workday import External, Workday

SOURCES: dict[str, Source] = {s.name: s for s in [Greenhouse(), Lever(), Ashby(), LinkedIn(), Indeed(),
                                                   Workday(), External()]}


def get(name: str) -> Source:
    return SOURCES[name]
