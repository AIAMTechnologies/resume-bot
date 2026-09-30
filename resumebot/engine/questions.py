"""Answer application form questions — from config, learned answers, or grounded LLM output.

If none of those produce a confident, factual answer, raise NeedsHuman so the question goes to
the review queue. The bot never guesses facts about you.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import Any

from sqlmodel import select

from .. import db
from ..config import answers
from ..llm import complete_json
from ..models import LearnedAnswer
from ..profile import master


class NeedsHuman(Exception):
    def __init__(self, question: str, proposed: str = "", options: list[str] | None = None):
        super().__init__(question)
        self.question = question
        self.proposed = proposed
        self.options = options or []


@dataclass
class Field:
    label: str
    kind: str = "text"       # text | email | tel | url | number | textarea | select | radio | checkbox | file | date
    options: list[str] | None = None
    required: bool = False


def norm_q(q: str) -> str:
    q = re.sub(r"\*|\(required\)|\(optional\)", "", q.lower())
    return re.sub(r"[^a-z0-9]+", " ", q).strip()


def _a(path: str) -> str:
    cur: Any = answers()
    for part in path.split("."):
        cur = (cur or {}).get(part, "")
    return str(cur or "")


# (pattern, answers.yaml path) — first match wins. Order matters.
RULES: list[tuple[str, str]] = [
    (r"^(legal )?first name|given name", "contact.first_name"),
    (r"^(legal )?last name|surname|family name", "contact.last_name"),
    (r"preferred (first )?name", "contact.preferred_name"),
    (r"^full name$|^name$|^your name", "__full_name"),
    (r"e ?mail", "contact.email"),
    (r"phone|mobile", "contact.phone"),
    (r"linkedin", "contact.linkedin"),
    (r"github", "contact.github"),
    (r"portfolio|personal (web)?site|website", "contact.portfolio"),
    (r"postal|zip", "contact.postal_code"),
    (r"^city|current city|location \(city\)", "contact.city"),
    (r"province|\bstate\b", "contact.province_state"),
    (r"^country", "contact.country"),
    (r"sponsor", "__sponsorship"),
    (r"(legally )?(authori[sz]ed|eligible|permit).{0,40}work", "__authorization"),
    (r"relocat", "work_authorization.willing_to_relocate"),
    (r"notice period", "logistics.notice_period"),
    (r"(earliest|when can you|available to) start|start date|availability", "logistics.earliest_start"),
    (r"hourly rate", "logistics.desired_hourly_contract_cad"),
    (r"salary|compensation|pay expectation", "__salary"),
    (r"years of (professional |total )?experience$|total years", "logistics.years_experience_total"),
    (r"highest (level of )?education|degree", "logistics.highest_education"),
    (r"\bgender\b|\bsex\b", "eeo.gender"),
    (r"\brace\b|ethnic", "eeo.race_ethnicity"),
    (r"veteran", "eeo.veteran_status"),
    (r"disabilit", "eeo.disability"),
    (r"pronoun", "eeo.pronouns"),
    # Work-arrangement questions only — not "experience with remote access / hybrid cloud / Office".
    (r"(open|willing|comfortable|able) to .{0,20}\bremote|\bremote(ly)? (work|role|position|job)|work(ing)? remotely",
     "logistics.open_to_remote"),
    (r"(open|willing|comfortable|able) to .{0,20}\bhybrid|\bhybrid (work|role|position|job|schedule|model|arrangement)",
     "logistics.open_to_hybrid"),
    (r"(open|willing|comfortable|able) to .{0,30}(\bon ?site|in (the )?office)|"
     r"\bon ?site (work|role|position|job)|(work|commute|come) (in|into|to) (the |our )?office",
     "logistics.open_to_onsite"),
]


def _special(key: str, question: str, job_location: str) -> str:
    us = bool(re.search(r"\b(us|u\.s\.|united states|usa|america)\b", question.lower() + " " + job_location.lower()))
    if key == "__full_name":
        return " ".join(x for x in [_a("contact.first_name"), _a("contact.last_name")] if x)
    if key == "__sponsorship":
        return _a("work_authorization.requires_sponsorship_us" if us else "work_authorization.requires_sponsorship_canada")
    if key == "__authorization":
        return _a("work_authorization.united_states" if us else "work_authorization.canada")
    if key == "__salary":
        return _a("logistics.desired_salary_usd" if us else "logistics.desired_salary_cad")
    return _a(key)


def closest_option(answer: str, options: list[str]) -> str | None:
    if not options:
        return answer
    low = {o.lower().strip(): o for o in options}
    if answer.lower().strip() in low:
        return low[answer.lower().strip()]
    # yes/no style
    for o in options:
        if answer.lower().startswith(("yes", "no")) and o.lower().startswith(answer.lower()[:2]):
            return o
    if "decline" in answer.lower():
        for o in options:
            if re.search(r"decline|prefer not|don.t wish|not to (say|answer|disclose)", o, re.I):
                return o
    m = difflib.get_close_matches(answer, options, n=1, cutoff=0.6)
    return m[0] if m else None


def from_rules(f: Field, job_location: str = "") -> str | None:
    q = norm_q(f.label)
    for pattern, key in RULES:
        if re.search(pattern, q):
            val = _special(key, f.label, job_location) if key.startswith("__") else _a(key)
            if val:
                return closest_option(val, f.options or [])
            return None
    return None


def from_memory(f: Field) -> str | None:
    q = norm_q(f.label)
    custom = {norm_q(k): str(v) for k, v in (answers().get("custom") or {}).items() if v}
    match = difflib.get_close_matches(q, list(custom), n=1, cutoff=0.88)
    if match:
        return closest_option(custom[match[0]], f.options or [])
    with db.session() as s:
        learned = list(s.exec(select(LearnedAnswer)))
    by_q = {la.question_norm: la for la in learned}
    match = difflib.get_close_matches(q, list(by_q), n=1, cutoff=0.9)
    if match:
        la = by_q[match[0]]
        la.uses += 1
        db.save(la)
        return closest_option(la.answer, f.options or [])
    return None


def remember(question: str, answer: str) -> None:
    q = norm_q(question)
    with db.session() as s:
        row = s.exec(select(LearnedAnswer).where(LearnedAnswer.question_norm == q)).first()
        row = row or LearnedAnswer(question_norm=q, question=question, answer=answer)
        row.answer = answer
        s.add(row)
        s.commit()


ANSWER_SYSTEM = """You fill in job application questions for a candidate using ONLY the facts
in their profile and standard answers. You are careful and honest.
- Factual questions (experience with X, years with Y, certifications, clearances, licenses):
  answer only if the profile clearly supports it. If the profile doesn't say, set grounded=false.
- Open-ended questions (why this company, describe a project): write a concise, specific answer
  (60-150 words) drawn from the profile; grounded=true.
- If options are given, the answer MUST be exactly one of the options.
- Never claim anything the profile doesn't support."""


async def from_llm(f: Field, job_title: str, company: str, description: str) -> tuple[str, bool, float]:
    std = {k: v for k, v in answers().items() if k != "eeo"}
    result = await complete_json(f"""STANDARD ANSWERS: {std}

JOB: {job_title} at {company}
{description[:4000]}

QUESTION: {f.label}
FIELD TYPE: {f.kind}
OPTIONS: {f.options or 'free text'}

Return {{"answer": "...", "grounded": true|false, "confidence": 0.0-1.0, "reason": "short"}}""",
        system=ANSWER_SYSTEM, max_tokens=800, context=master.prompt_context(),
        fast=f.kind != "textarea")  # written answers are read by people; use the main model
    return str(result.get("answer", "")), bool(result.get("grounded")), float(result.get("confidence", 0))


class Answerer:
    """Per-application answerer. Records every answer and where it came from."""

    def __init__(self, job_title: str, company: str, description: str, location: str = "",
                 min_confidence: float = 0.8):
        self.job_title, self.company, self.description, self.location = job_title, company, description, location
        self.min_confidence = min_confidence
        self.log: dict[str, dict[str, str]] = {}

    async def __call__(self, f: Field) -> str:
        # Your own custom/approved answers beat the generic rules.
        for origin, fn in (("memory", lambda: from_memory(f)), ("config", lambda: from_rules(f, self.location))):
            val = fn()
            if val:
                self.log[f.label] = {"answer": val, "origin": origin}
                return val
        answer, grounded, conf = await from_llm(f, self.job_title, self.company, self.description)
        if f.options:
            answer = closest_option(answer, f.options) or ""
        if answer and grounded and conf >= self.min_confidence:
            self.log[f.label] = {"answer": answer, "origin": f"llm ({conf:.2f})"}
            return answer
        if not f.required:
            self.log[f.label] = {"answer": "", "origin": "skipped optional"}
            return ""
        raise NeedsHuman(f.label, answer, f.options)
