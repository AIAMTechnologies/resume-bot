"""Generic application-form filler shared by every source.

Scans a form container for fields, works out each field's label/type/options in the page,
asks the Answerer, and fills with human-like input. Collects every unanswerable required
question and raises NeedsInput once, so you answer them all in one review.
"""
from __future__ import annotations

import asyncio
import random
import re
from typing import TYPE_CHECKING

from playwright.async_api import TimeoutError as PlaywrightTimeout

from .. import db
from ..config import answers
from ..engine.questions import Field, NeedsHuman, from_memory
from .base import ApplyContext, NeedsInput

if TYPE_CHECKING:
    from playwright.async_api import Locator

SCAN_JS = r"""
([selector, start]) => {
  const roots = [...document.querySelectorAll(selector)];
  const root = { querySelectorAll: (q) => { const seen = new Set(), out = [];
    for (const r of (roots.length ? roots : [document.body])) for (const el of r.querySelectorAll(q))
      if (!seen.has(el)) { seen.add(el); out.push(el); }
    return out; } };
  const vis = el => { if (!el) return false; const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const clean = t => (t || '').replace(/\s+/g, ' ').trim().slice(0, 400);
  const textOf = el => { const c = el.cloneNode(true);
    c.querySelectorAll('input,select,textarea,option,button,[role=listbox]').forEach(x => x.remove()); return clean(c.innerText); };
  const labelFor = el => {
    let t = '';
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) t = textOf(l); }
    if (!t && el.getAttribute('aria-labelledby'))
      t = el.getAttribute('aria-labelledby').split(/\s+/).map(i => textOf(document.getElementById(i) || document.createElement('i'))).join(' ');
    if (!t) t = el.getAttribute('aria-label') || '';
    if (!t) { const l = el.closest('label'); if (l) t = textOf(l); }
    if (!t) { let p = el.parentElement; for (let i = 0; i < 4 && p && !t; i++) { t = textOf(p); p = p.parentElement; } }
    if (!t) t = el.placeholder || el.name || '';
    return clean(t);
  };
  const groupLabel = el => {
    const fs = el.closest('fieldset, [role=radiogroup], [role=group]');
    if (fs) { const lg = fs.querySelector('legend, [id$=label], label:not(:has(input))');
      if (lg) return textOf(lg);
      if (fs.getAttribute('aria-labelledby')) { const x = document.getElementById(fs.getAttribute('aria-labelledby')); if (x) return textOf(x); }
      if (fs.getAttribute('aria-label')) return clean(fs.getAttribute('aria-label')); }
    let p = el.parentElement; for (let i = 0; i < 6 && p; i++) {
      const prev = p.previousElementSibling; if (prev && textOf(prev)) return textOf(prev); p = p.parentElement; }
    return '';
  };
  const isReq = el => {
    const entry = el.closest('.ashby-application-form-field-entry');
    const label = entry?.querySelector('label');
    return !!(el.required || el.getAttribute('aria-required') === 'true' ||
      /\*\s*$/.test(labelFor(el)) || /\*\s*$/.test(groupLabel(el)) ||
      el.closest('[class*=required i]') || label?.matches('[class*=required i]'));
  };
  const isPlaceholder = o => !o || !o.value || /^(select|choose|--|please)/i.test(o.text.trim());
  const out = []; const groups = {}; let n = start;
  const stableSelector = el => {
    if (el.id) return `#${CSS.escape(el.id)}`;
    if (el.name) return `${el.tagName.toLowerCase()}[name="${CSS.escape(el.name)}"]`;
    const entry = el.closest('[data-field-path]');
    if (entry) return `[data-field-path="${CSS.escape(entry.getAttribute('data-field-path'))}"] ${el.tagName.toLowerCase()}`;
    return `[data-rb-id="${el.getAttribute('data-rb-id')}"]`;
  };
  root.querySelectorAll('input, select, textarea').forEach(el => {
    const type = (el.getAttribute('type') || el.tagName).toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset', 'search'].includes(type)) return;
    if (el.hasAttribute('data-rb-done')) return;
    // Hidden companion inputs used only for validation (react-select's "requiredInput" next to each
    // dropdown) are not fields a person fills; typing into them hangs.
    if (!['radio', 'checkbox', 'file'].includes(type) &&
        (el.getAttribute('aria-hidden') === 'true' || /requiredinput/i.test(el.className || ''))) return;
    const shown = vis(el) || (el.labels && [...el.labels].some(vis)) || type === 'file';
    if (!shown || el.disabled) return;
    const id = String(n++); el.setAttribute('data-rb-id', id);
    if (type === 'radio' || (type === 'checkbox' && el.name && document.querySelectorAll(`input[type=checkbox][name="${CSS.escape(el.name)}"]`).length > 1)) {
      const key = type + ':' + (el.name || groupLabel(el));
      groups[key] = groups[key] || { kind: type === 'radio' ? 'radio' : 'checkgroup', label: groupLabel(el) || el.name,
        required: false, options: [], ids: [], selectors: [], checked: [] };
      const g = groups[key]; g.options.push(labelFor(el)); g.ids.push(id); g.selectors.push(stableSelector(el)); if (el.checked) g.checked.push(labelFor(el));
      g.required = g.required || isReq(el);
      return;
    }
    const f = { id, kind: el.tagName === 'SELECT' ? 'select' : el.tagName === 'TEXTAREA' ? 'textarea' : type,
      label: labelFor(el), required: isReq(el),
      value: el.type === 'checkbox' ? String(el.checked)
        : el.tagName === 'SELECT' ? (isPlaceholder(el.options[el.selectedIndex]) ? '' : el.value) : (el.value || ''),
      role: el.getAttribute('role') || '', name: el.name || '', accept: el.getAttribute('accept') || '', selector: stableSelector(el) };
    if (el.tagName === 'SELECT') f.options = [...el.options].filter(o => !isPlaceholder(o)).map(o => o.text.trim());
    if (f.role === 'combobox' || el.getAttribute('aria-autocomplete') === 'list') f.kind = 'combobox';
    out.push(f);
  });
  // Ashby yes/no controls are buttons backed by an invisible checkbox.
  root.querySelectorAll('.ashby-application-form-input-yesno').forEach(group => {
    const buttons = [...group.querySelectorAll('button[data-option]')].filter(vis);
    if (!buttons.length) return;
    const entry = group.closest('.ashby-application-form-field-entry');
    const ids = buttons.map(el => { const id = String(n++); el.setAttribute('data-rb-id', id); return id; });
    out.push({kind: 'radio', label: textOf(entry?.querySelector('label') || group),
      required: isReq(buttons[0]), options: buttons.map(el => clean(el.innerText)), ids, selectors: buttons.map(stableSelector),
      checked: buttons.filter(el => el.getAttribute('aria-pressed') === 'true').map(el => clean(el.innerText))});
  });
  // Generic button-style choices (any site): a labelled field whose only controls are 2–4 short
  // buttons like Yes / No / Not sure. Skip groups already captured above.
  const CHOICE = /^(yes|no|not sure|maybe|prefer not to say|n\/a)$/i;
  document.querySelectorAll('[data-rb-group]').forEach(e => e.removeAttribute('data-rb-group'));
  root.querySelectorAll('button[type=button], [role=radio]').forEach(btn => {
    if (btn.closest('.ashby-application-form-input-yesno') || !CHOICE.test(clean(btn.innerText))) return;
    const holder = btn.parentElement;
    if (!holder || holder.hasAttribute('data-rb-group')) return;
    const buttons = [...holder.querySelectorAll(':scope > button, :scope > [role=radio]')].filter(vis);
    if (buttons.length < 2 || buttons.length > 4 || !buttons.every(b => CHOICE.test(clean(b.innerText)))) return;
    holder.setAttribute('data-rb-group', '1');
    let field = holder.parentElement, label = '';
    for (let i = 0; i < 4 && field && !label; i++, field = field.parentElement) {
      const l = field.querySelector('label, legend, [id$=label]');
      if (l && !holder.contains(l)) label = textOf(l);
    }
    const ids = buttons.map(el => { const id = String(n++); el.setAttribute('data-rb-id', id); return id; });
    out.push({kind: 'radio', label: label || 'Yes/No question', required: /\*\s*$/.test(label) || isReq(buttons[0]),
      options: buttons.map(el => clean(el.innerText)), ids, selectors: buttons.map(stableSelector),
      checked: buttons.filter(el => el.getAttribute('aria-pressed') === 'true' || el.getAttribute('aria-checked') === 'true')
        .map(el => clean(el.innerText))});
  });
  Object.values(groups).forEach(g => out.push(g));
  return out;
}
"""

CONSENT_RE = re.compile(r"(i )?(agree|consent|acknowledge|certify|confirm).{0,80}(privacy|terms|policy|accurate|true|processing|data)", re.I)
SKIP_CHECKBOX_RE = re.compile(r"follow|newsletter|marketing|job alerts?|subscribe|updates", re.I)


def _consent_answer(label: str) -> bool | None:
    """Your answer from review for a consent box: True = check, False = leave it, None = not answered yet."""
    answer = from_memory(Field(label, "checkbox"))
    if not answer:
        return None
    return bool(re.match(r"\s*(check|yes|y\b|agree|i agree|ok|true)", answer, re.I))


def _file_role(label: str, name: str, selector: str = "") -> str:
    # Greenhouse labels the upload button just "Attach"; its id (#resume, #cover_letter) says what it is.
    s = f"{label} {name} {selector}".lower()
    if re.search(r"cover", s):
        return "cover"
    if re.search(r"resume|cv|curriculum", s):
        return "resume"
    return "other"


async def _clickable(ctx: ApplyContext, loc: "Locator") -> "Locator":
    """Styled radios/checkboxes hide the <input>; click its label instead."""
    if await loc.is_visible():
        return loc
    label = loc.locator("xpath=ancestor::label[1]")
    if await label.count():
        return label
    el_id = await loc.get_attribute("id")
    if el_id:
        return ctx.page.locator(f'label[for="{el_id}"]')
    return loc


async def _combobox_options(ctx: ApplyContext, loc: "Locator") -> list[str]:
    await ctx.human.click(loc)
    await asyncio.sleep(random.uniform(0.4, 0.9))
    opts = await ctx.page.locator("[role=option]:visible, [role=listbox] [class*=option]:visible").all_inner_texts()
    await ctx.page.keyboard.press("Escape")
    return [o.strip() for o in opts if o.strip()][:60]


async def _pick_combobox(ctx: ApplyContext, loc: "Locator", answer: str) -> bool:
    await ctx.human.type(loc, answer[:40], typos=False)
    await asyncio.sleep(random.uniform(0.6, 1.2))
    option = ctx.page.locator("[role=option]:visible, [role=listbox] [class*=option]:visible").filter(has_text=answer).first
    if await option.count():
        await ctx.human.click(option)
        return True
    await ctx.page.keyboard.press("Escape")
    return False


AUTOFILL_RE = re.compile(r"autofill|auto-fill|parse your resume|import from", re.I)
BUSY_RE = r"parsing your resume|uploading|autofilling"


async def _scan(ctx: ApplyContext, root_selector: str, start: int) -> list[dict]:
    return await ctx.page.evaluate(SCAN_JS, [root_selector, start])


def _locator(ctx: ApplyContext, field: dict, index: int | None = None) -> "Locator":
    """Find the current DOM node after a framework replaces a scanned form field."""
    selector = (field.get("selectors") or [])[index] if index is not None else field.get("selector")
    fallback = f'[data-rb-id="{field["ids"][index] if index is not None else field["id"]}"]'
    return ctx.page.locator(selector or fallback).first


async def _settle(ctx: ApplyContext, timeout: float = 20) -> None:
    """Wait for upload/parse spinners to finish before touching the form again."""
    rx = re.compile(BUSY_RE, re.I)
    for _ in range(int(timeout * 2)):
        try:
            if not rx.search(await ctx.page.locator("body").inner_text(timeout=2000)):
                break
        except Exception:
            break
        await asyncio.sleep(0.5)
    await ctx.human.pause(0.8, 1.8)


def _field_key(f: dict) -> str:
    """Identity that survives re-scans (temporary data-rb-id markers change every scan)."""
    stable = [x for x in [f.get("selector"), *(f.get("selectors") or [])] if x and "data-rb-id" not in x]
    return f"{f.get('kind')}|{f.get('label')}|{','.join(f.get('options') or [])}|{','.join(stable)}"


async def fill_form(ctx: ApplyContext, root_selector: str = "body") -> dict[str, str]:
    """Fill every visible field in all elements matching root_selector. Returns {label: answer}.

    Pass 1 uploads files (pages often re-render after an upload); pass 2 re-scans and fills the rest.
    """
    consent_ok = bool(answers().get("application_consent"))
    filled: dict[str, str] = {}
    pending: list[tuple[str, str, list[str]]] = []

    files = [f for f in await _scan(ctx, root_selector, 0) if f["kind"] == "file"]
    for f in files:
        label = f.get("label", "")
        if AUTOFILL_RE.search(label):
            continue  # the bot fills every field itself; resume parsers inject wrong values
        role = _file_role(label, f.get("name", ""), f.get("selector", ""))
        loc = ctx.page.locator(f'[data-rb-id="{f["id"]}"]')
        if role == "resume":
            await ctx.human.upload(loc, str(ctx.materials.resume_pdf))
            filled[label] = ctx.materials.resume_pdf.name
        elif role == "cover" and ctx.materials.cover_letter_pdf:
            await ctx.human.upload(loc, str(ctx.materials.cover_letter_pdf))
            filled[label] = ctx.materials.cover_letter_pdf.name
        await loc.evaluate("el => el.setAttribute('data-rb-done', '1')")
    if files:
        await _settle(ctx)

    # Re-scan after filling: answering one question can reveal follow-ups ("Yes" → "Where?").
    seen: set[str] = set()
    for round_ in range(3):
        fields = [f for f in await _scan(ctx, root_selector, 10_000 + 1_000 * round_)
                  if f["kind"] != "file" and _field_key(f) not in seen]
        if round_ and not any(not f.get("value") and not f.get("checked") for f in fields):
            break
        for f in fields:
            seen.add(_field_key(f))
            kind, label = f["kind"], f.get("label", "")
            try:
                if kind == "file":
                    role = _file_role(label, f.get("name", ""), f.get("selector", ""))
                    loc = ctx.page.locator(f'[data-rb-id="{f["id"]}"]')
                    if role == "resume":
                        await ctx.human.upload(loc, str(ctx.materials.resume_pdf))
                        filled[label] = ctx.materials.resume_pdf.name
                    elif role == "cover" and ctx.materials.cover_letter_pdf:
                        await ctx.human.upload(loc, str(ctx.materials.cover_letter_pdf))
                        filled[label] = ctx.materials.cover_letter_pdf.name
                    continue

                if kind in ("radio", "checkgroup"):
                    if f.get("checked"):
                        continue
                    answer = await ctx.answer(Field(label, "radio", f["options"], f["required"]))
                    if not answer:
                        continue
                    if answer not in f["options"]:
                        raise NeedsHuman(label, answer, f["options"])
                    idx = f["options"].index(answer)
                    await ctx.human.click(await _clickable(ctx, _locator(ctx, f, idx)))
                    filled[label] = answer
                    continue

                loc = _locator(ctx, f)
                if kind == "checkbox":
                    checked = f.get("value") == "true"
                    if SKIP_CHECKBOX_RE.search(label):
                        if checked:
                            await ctx.human.click(await _clickable(ctx, loc))  # un-follow / un-subscribe
                        continue
                    if CONSENT_RE.search(label):
                        agree = consent_ok or _consent_answer(label)
                        if agree is None:
                            raise NeedsHuman(label, "check", ["check", "leave unchecked"])
                        if agree and not checked:
                            await ctx.human.click(await _clickable(ctx, loc))
                        filled[label] = "checked" if agree else "unchecked"
                        continue
                    if f["required"] and not checked:
                        answer = await ctx.answer(Field(label, "checkbox", ["Yes", "No"], True))
                        if answer.lower().startswith("y"):
                            await ctx.human.click(await _clickable(ctx, loc))
                        filled[label] = answer
                    continue

                if f.get("value"):  # prefilled (e.g. LinkedIn profile data) — leave as-is
                    continue
                if kind == "select":
                    answer = await ctx.answer(Field(label, "select", f.get("options") or [], f["required"]))
                    if answer:
                        await ctx.human.select(loc, answer)
                        filled[label] = answer
                elif kind == "combobox":
                    options = await _combobox_options(ctx, loc)
                    answer = await ctx.answer(Field(label, "select" if options else "text", options or None, f["required"]))
                    if answer:
                        if not await _pick_combobox(ctx, loc, answer):
                            raise NeedsHuman(label, answer, options)
                        filled[label] = answer
                else:
                    answer = await ctx.answer(Field(label, kind, None, f["required"]))
                    if answer:
                        await ctx.human.type(loc, answer, typos=kind == "textarea")
                        filled[label] = answer
            except NeedsHuman as nh:
                pending.append((nh.question, nh.proposed, nh.options))
            except PlaywrightTimeout:
                # One stubborn field must not sink the whole application.
                if f.get("required"):
                    pending.append((label.rstrip("* ").strip() or "A required field", "", f.get("options") or []))
                else:
                    db.log(f"Skipped optional field that wouldn't respond: {label[:80]}", level="warning", kind="apply")
    if pending:
        unique = {q: (q, proposed, options) for q, proposed, options in reversed(pending)}
        raise NeedsInput([unique[q] for q in dict.fromkeys(q for q, _, _ in pending)])
    return filled


async def click_button(ctx: ApplyContext, *names: str, root: str = "body") -> bool:
    """Click the first visible button/link matching any of the names (regex, case-insensitive)."""
    scope = ctx.page.locator(root).first
    for name in names:
        btn = scope.get_by_role("button", name=re.compile(name, re.I))
        if await btn.count() and await btn.first.is_visible():
            await ctx.human.click(btn.first)
            return True
        link = scope.get_by_role("link", name=re.compile(name, re.I))
        if await link.count() and await link.first.is_visible():
            await ctx.human.click(link.first)
            return True
    return False


async def page_has_text(ctx: ApplyContext, pattern: str, timeout: float = 15) -> bool:
    rx = re.compile(pattern, re.I)
    for _ in range(int(timeout * 2)):
        try:
            if rx.search(await ctx.page.locator("body").inner_text(timeout=2000)):
                return True
        except Exception:
            pass
        await asyncio.sleep(0.5)
    return False
