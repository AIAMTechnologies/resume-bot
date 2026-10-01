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
    (r"(location|city).{0,4}(province|state)|city and (province|state)", "__city_region"),
    (r"^(legal )?first name|given name", "contact.first_name"),
    (r"^(legal )?last name|surname|family name", "contact.last_name"),
    (r"preferred (first )?name", "contact.preferred_name"),
    (r"^full name$|^name$|^your name", "__full_name"),
    (r"e ?mail", "contact.email"),
    (r"phone|mobile", "contact.phone"),
    (r"linkedin", "contact.linkedin"),
    (r"github", "contact.github"),
    (r"portfolio|personal (web)?site|website", "__website"),
    (r"(receive|subscribe|sign up for|opt.?in).{0,40}(marketing|newsletter|promotional|updates about careers)|marketing communications|(receive|consent to).{0,40}(text messages?|sms)", "__no_marketing"),
    (r"address line 2|apartment|suite|unit number", "__address_line2"),
    (r"address line 1|street address|^address$|mailing address|home address", "contact.address"),
    (r"languages? .{0,20}(speak|fluent)|fluent.{0,20}languages?|what languages", "__languages"),
    (r"(i )?(have read|agree|consent|acknowledge|understand).{0,120}(privacy|policy|guidelines|terms|notice|processing)", "__consent"),
    (r"postal|zip", "contact.postal_code"),
    (r"^city|current city|^(current )?location( city)?$|what city|city of residence|where do you (currently )?(live|reside)", "contact.city"),
    (r"where are you (currently )?(located|based)|^(current )?location of residence|where (are you|do you) (currently )?(located|based|live)|^current (city|location)|where (do|will) you plan (on|to) (work|working) from", "__city_region"),
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
    (r"\brace\b|ethnic|hispanic|latin[oa]x?", "eeo.race_ethnicity"),
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
    from .geography import is_us_location
    us = is_us_location(question) or is_us_location(job_location)
    if re.search(r"\bcanada\b", question, re.I):
        us = False
    if key == "__no_marketing":  # never opt in to marketing
        return "No"
    if key == "__address_line2":  # always optional; leave blank when there's none
        return _a("contact.address_line2")
    if key == "__website":  # no portfolio → GitHub is the next best public site
        return _a("contact.portfolio") or _a("contact.github")
    if key == "__languages":
        langs = answers().get("contact", {}).get("languages") or ["English"]
        return ", ".join(langs) if isinstance(langs, list) else str(langs)
    if key == "__consent":  # same permission as consent checkboxes (answers.yaml: application_consent)
        return "Yes" if answers().get("application_consent") else ""
    if key == "__city_region":
        return ", ".join(x for x in [_a("contact.city"), _a("contact.province_state")] if x)
    if key == "__full_name":
        return " ".join(x for x in [_a("contact.first_name"), _a("contact.last_name")] if x)
    if key == "__sponsorship":
        return _a("work_authorization.requires_sponsorship_us" if us else "work_authorization.requires_sponsorship_canada")
    if key == "__authorization":
        return _a("work_authorization.united_states" if us else "work_authorization.canada")
    if key == "__salary":
        return _a("logistics.desired_salary_usd" if us else "logistics.desired_salary_cad")
    return _a(key)



# Personal screening questions (answers.yaml → screening). Matched on the normalized label; first match
# wins. A blank answer stops and asks you — these facts are never guessed by the AI.
SCREENING_RULES: list[tuple[str, str]] = [
    (r"arbitration", "agree_to_arbitration"),
    (r"background (check|screening)s? .{0,80}third.party|third.party .{0,80}background|strider", "third_party_background_screening"),
    (r"close relative of a government official|relative of a (government|public) official", "relative_of_government_official"),
    (r"(current|former) (government|public) official|government official in the last", "government_official"),
    (r"referred .{0,60}senior leader|senior leader .{0,80}referred", "referred_by_client_leader"),
    (r"conflict of interest|financial interest in", "conflict_of_interest"),
    (r"(family|relatives?|related to|personal relationships?|domestic partner).{0,80}(work|employed|at )", "relatives_at_company"),
    (r"criminal (record|conviction|offen)|convicted|pending (criminal )?charges", "criminal_convictions"),
    (r"non.?compete|non.?solicit|post.?employment restrictions?|restrictive covenant", "non_compete"),
    (r"outside business|side business|board roles?|advisory .{0,20}roles?", "outside_business"),
    (r"\bfinra\b|series (7|24|27|63|65|66)", "finra_licenses"),
    (r"(interviewed|applied) (with|at|for) .{0,40}(before|previously)|ever interviewed", "interviewed_here_before"),
    (r"(previously|ever|currently).{0,30}(worked|employed|consult(ed|ant)|contractor).{0,40}(at|by|for|with)\b|"
     r"been employed by|worked for .{0,40} before", "previously_employed_here"),
    (r"(member|contributor) .{0,30}(our )?communit", "community_contributor"),
    (r"\bai policy\b|policy on (the )?use of ai|use of ai .{0,40}application", "ai_policy_agreement"),
    (r"(monday|mon) .{0,20}(friday|fri) .{0,40}\d|on.?call|weekends?( and|/| or) holidays?|weekend shifts?|"
     r"(shifts?|rotation|schedule|hours) .{0,80}(comfortable|committed|willing|able|available)|"
     r"(comfortable|committed|willing|able|available) .{0,60}(shifts?|rotation|evenings?|nights?|weekends?|overnight)", "fixed_shift_and_on_call"),
    (r"sexual orientation", "sexual_orientation"),
    (r"name pronunciation|pronounce your name", "name_pronunciation"),
    (r"^school( name)?$|^(university|college|institution)( name)?$|school you attended", "__school"),
    (r"^discipline$|field of study|^major$|area of study", "__discipline"),
]
AGREEMENT_KEYS = {"agree_to_arbitration", "third_party_background_screening", "ai_policy_agreement"}
NONE_OPTION_RE = re.compile(r"^(n/?a|none|not applicable)\b|^no\b|do not hold|don.t hold", re.I)


def _employers() -> list[str]:
    from ..models import ProfileItem
    with db.session() as s:
        rows = s.exec(select(ProfileItem.organization).where(ProfileItem.kind == "experience")).all()
    names = set()
    for org in rows:
        for part in re.split(r"[(/]|\bvia\b", org or ""):
            if norm_q(part):
                names.add(norm_q(part))
    return sorted(names)


def screening_answer(f: "Field", company: str) -> tuple[bool, str]:
    """(matched, answer). matched with "" answer → you must answer it (it's a fact about you)."""
    q = norm_q(f.label)
    for pattern, key in SCREENING_RULES:
        if not re.search(pattern, q):
            continue
        if key == "__school":
            val = _a("education.school")
        elif key == "__discipline":
            val = _a("education.discipline")
        else:
            val = _a(f"screening.{key}").strip()
        if key == "previously_employed_here" and val.lower() == "auto":
            targets = [norm_q(company)] + [n for n in _employers() if f" {n} " in f" {q} "]
            val = "Yes" if any(t and t in _employers() for t in targets) else "No"
        if not val:
            return True, ""
        options = f.options or []
        if key in AGREEMENT_KEYS and val.lower().startswith("n"):
            return True, ""  # declined: the caller turns this job into a manual card
        if key in AGREEMENT_KEYS and options:
            if val.lower().startswith("y"):
                agree = [o for o in options if re.search(r"\b(agree|acknowledge|consent|yes|accept)", o, re.I)
                         and not re.search(r"\b(do not|don.t|disagree|decline)\b", o, re.I)]
                return True, (agree[0] if agree else closest_option(val, options) or "")
        if options and val.lower() in ("no", "none", "n/a"):
            pick = closest_option(val, options)
            if not pick:
                pick = next((o for o in options if NONE_OPTION_RE.search(o.strip())), None)
            return True, pick or ""
        return True, (closest_option(val, options) if options else val) or ""
    return False, ""


def key_is_legal(label: str) -> bool:
    q = norm_q(label)
    return any(re.search(p, q) for p, k in SCREENING_RULES if k in AGREEMENT_KEYS)


def _declined_agreement(label: str) -> bool:
    q = norm_q(label)
    for p, k in SCREENING_RULES:
        if k in AGREEMENT_KEYS and re.search(p, q):
            return _a(f"screening.{k}").strip().lower().startswith("n")
    return False


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
    synonyms = {"male": ["man", "cisgender man"], "female": ["woman", "cisgender woman"]}
    for alt in synonyms.get(answer.lower().strip(), []):
        if alt in low:
            return low[alt]
    if "decline" in answer.lower():
        for o in options:
            if re.search(r"decline|prefer not|don.t wish|not to (say|answer|disclose)", o, re.I):
                return o
    contained = _containing_option(answer, options)
    if contained:
        return contained
    m = difflib.get_close_matches(answer, options, n=1, cutoff=0.6)
    return m[0] if m else None


PROVINCES = {"ON": "Ontario", "BC": "British Columbia", "AB": "Alberta", "QC": "Quebec", "MB": "Manitoba",
             "SK": "Saskatchewan", "NS": "Nova Scotia", "NB": "New Brunswick", "NL": "Newfoundland",
             "PE": "Prince Edward Island"}


def _containing_option(answer: str, options: list[str]) -> str | None:
    """'Toronto' -> 'Toronto, Ontario, Canada' (not 'Toronto, Ohio, United States').

    Picks options containing the answer as whole words; ties go to the option that also
    mentions your province/state or country from answers.yaml.
    """
    key = norm_q(answer)
    if len(key) < 3 or key in ("yes", "no"):
        return None
    hits = [o for o in options if re.search(rf"\b{re.escape(key)}\b", norm_q(o))]
    if not hits:
        return None
    region = _a("contact.province_state")
    context = [norm_q(x) for x in (region, PROVINCES.get(region.upper(), ""), _a("contact.country")) if x]
    return max(hits, key=lambda o: (sum(f" {c} " in f" {norm_q(o)} " for c in context), -len(o)))


def _clean_url(value: str) -> str:
    """Drop tracking/query strings from profile URLs (e.g. LinkedIn's ?isSelfProfile=true)."""
    return value.split("?")[0] if value.startswith(("http://", "https://")) and "linkedin.com" in value else value


def from_rules(f: Field, job_location: str = "") -> str | None:
    q = norm_q(f.label)
    for pattern, key in RULES:
        if re.search(pattern, q):
            val = _special(key, f.label, job_location) if key.startswith("__") else _a(key)
            val = _clean_url(val)
            if val and f.options and ", " in val:  # several answers (e.g. languages): match each to an option
                parts = [closest_option(p, f.options) for p in val.split(", ")]
                return ", ".join(p for p in parts if p) or None
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


HEARD_RE = re.compile(r"how did you (hear|find|learn|come across)|where did you (hear|find|learn|see)|what (brought|led) you to|source of (your )?application|referral source", re.I)
# What to look for in the options, by where the bot actually found the job (truthful answer first).
HEARD_PREFERENCES = {
    "linkedin": [r"linkedin.*(job|post)", r"linkedin"],
    "indeed": [r"indeed"],
    "": [r"careers? (page|site|website)|company (web)?site|our website|corporate website",
         r"job board|online job|job posting|internet|online"],
}


def heard_about(f: Field, source: str) -> str:
    """'How did you hear about us?' → the option matching where the job was actually found."""
    if not HEARD_RE.search(f.label):
        return ""
    prefs = HEARD_PREFERENCES.get(source, []) + HEARD_PREFERENCES[""] + [r"^other$"]
    if not f.options:
        return {"linkedin": "LinkedIn", "indeed": "Indeed"}.get(source, "Company careers page")
    for pattern in prefs:
        for option in f.options:
            if re.search(pattern, option, re.I):
                return option
    return ""


class Answerer:
    """Per-application answerer. Records every answer and where it came from."""

    def __init__(self, job_title: str, company: str, description: str, location: str = "",
                 min_confidence: float = 0.8, source: str = ""):
        self.job_title, self.company, self.description, self.location = job_title, company, description, location
        self.source = source
        self.min_confidence = min_confidence
        self.log: dict[str, dict[str, str]] = {}

    async def __call__(self, f: Field) -> str:
        heard = heard_about(f, self.source)
        if heard:
            self.log[f.label] = {"answer": heard, "origin": f"where the job was found ({self.source})"}
            return heard
        if norm_q(f.label).startswith(("address line 2", "home address line 2", "apartment", "suite")) and \
                not _a("contact.address_line2"):
            self.log[f.label] = {"answer": "", "origin": "left blank (optional)"}
            return ""
        # Your own custom/approved answers beat the generic rules.
        val = from_memory(f)
        if val:
            self.log[f.label] = {"answer": val, "origin": "memory"}
            return val
        matched, val = screening_answer(f, self.company)
        if matched:
            if val:
                self.log[f.label] = {"answer": val, "origin": "answers.yaml screening"}
                return val
            if key_is_legal(f.label) and _declined_agreement(f.label):
                from ..sources.base import ManualRequired
                raise ManualRequired(f"you chose not to auto-agree: {f.label[:80]}")
            if not f.required:
                self.log[f.label] = {"answer": "", "origin": "skipped optional (screening blank)"}
                return ""
            raise NeedsHuman(f.label, "", f.options)  # a fact about you: never guessed
        val = from_rules(f, self.location)
        if val:
            self.log[f.label] = {"answer": val, "origin": "config"}
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
