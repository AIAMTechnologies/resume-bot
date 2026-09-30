"""The master list: merge extracted items, dedupe, and render for prompts."""
from __future__ import annotations

import difflib
import re
from typing import Iterable

from sqlmodel import select

from .. import db
from ..models import ProfileItem, utcnow

KINDS = ["summary", "experience", "project", "education", "certification", "skill", "achievement"]


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def fingerprint(kind: str, title: str, org: str) -> str:
    return f"{kind}|{norm(title)}|{norm(org)}"


def _similar(a: str, b: str, threshold: float = 0.85) -> bool:
    return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio() >= threshold


def _merge_list(existing: list[str], new: Iterable[str], threshold: float = 0.85) -> list[str]:
    out = list(existing)
    for item in new:
        item = (item or "").strip()
        if item and not any(_similar(item, e, threshold) for e in out):
            out.append(item)
    return out


def _find_match(items: list[ProfileItem], cand: dict) -> ProfileItem | None:
    fp = fingerprint(cand["kind"], cand.get("title", ""), cand.get("organization", ""))
    for it in items:
        if it.fingerprint == fp:
            return it
    for it in items:
        if it.kind != cand["kind"]:
            continue
        if _similar(it.title, cand.get("title", ""), 0.8) and (
            not it.organization or not cand.get("organization")
            or _similar(it.organization, cand.get("organization", ""), 0.75)
        ):
            return it
    return None


def merge_items(candidates: list[dict], source_name: str) -> int:
    """Upsert extracted items into the master list. Returns number of new items."""
    added = 0
    with db.session() as s:
        existing = list(s.exec(select(ProfileItem)))
        for cand in candidates:
            kind = cand.get("kind", "").lower()
            if kind not in KINDS:
                continue
            cand["kind"] = kind
            match = _find_match(existing, cand)
            if match:
                match.bullets = _merge_list(match.bullets, cand.get("bullets", []))
                match.skills = _merge_list(match.skills, cand.get("skills", []), 0.95)
                match.sources = _merge_list(match.sources, [source_name], 1.0)
                for field in ("location", "start", "end", "url", "organization"):
                    if not getattr(match, field) and cand.get(field):
                        setattr(match, field, cand[field])
                match.updated_at = utcnow()
                s.add(match)
            else:
                item = ProfileItem(
                    kind=kind,
                    title=cand.get("title", ""),
                    organization=cand.get("organization", ""),
                    location=cand.get("location", ""),
                    start=cand.get("start", ""),
                    end=cand.get("end", ""),
                    url=cand.get("url", ""),
                    bullets=_merge_list([], cand.get("bullets", [])),
                    skills=_merge_list([], cand.get("skills", []), 0.95),
                    sources=[source_name],
                    fingerprint=fingerprint(kind, cand.get("title", ""), cand.get("organization", "")),
                )
                s.add(item)
                existing.append(item)
                added += 1
        s.commit()
    return added


def all_items(include_hidden: bool = False) -> list[ProfileItem]:
    with db.session() as s:
        q = select(ProfileItem)
        if not include_hidden:
            q = q.where(ProfileItem.hidden == False)  # noqa: E712
        items = list(s.exec(q))
    def recency(i: ProfileItem) -> str:
        end = "9999" if i.end.lower() in ("present", "current") else i.end
        return f"{end or i.start}|{i.start}"

    # Newest first within each kind, like a resume.
    items.sort(key=recency, reverse=True)
    return sorted(items, key=lambda i: KINDS.index(i.kind) if i.kind in KINDS else 99)


def all_skills() -> list[str]:
    skills: list[str] = []
    for it in all_items():
        if it.kind == "skill":
            skills = _merge_list(skills, [it.title, *it.skills], 0.95)
        else:
            skills = _merge_list(skills, it.skills, 0.95)
    return skills


def render(items: list[ProfileItem] | None = None, with_ids: bool = True) -> str:
    """Compact text version of the master list for prompts."""
    items = items if items is not None else all_items()
    lines: list[str] = []
    for kind in KINDS:
        group = [i for i in items if i.kind == kind]
        if not group:
            continue
        lines.append(f"\n## {kind.upper()}")
        for i in group:
            head = " | ".join(x for x in [i.title, i.organization, i.location,
                                          f"{i.start}–{i.end}" if (i.start or i.end) else "", i.url] if x)
            lines.append(f"[{i.id}] {head}" if with_ids else head)
            for b in i.bullets:
                lines.append(f"  - {b}")
            if i.skills:
                lines.append(f"  skills: {', '.join(i.skills)}")
    return "\n".join(lines).strip()


def prompt_context() -> str:
    """The profile block shared by scoring, answers and letters. Byte-identical between calls so
    the LLM backend can serve it from its prompt cache."""
    return f"CANDIDATE PROFILE:\n{render(with_ids=False)[:20000]}"


def contact() -> dict:
    from ..config import answers
    return answers().get("contact", {})
