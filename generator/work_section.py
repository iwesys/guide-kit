"""
work_section.py — the "Работа" slot in the daily guide (WP-483 Phase 4,
Professional role, CONCEPT-full-architecture.md §3-4 Step 3b).

Read-only: this module imports nothing that writes, and never touches the
files it lists — a person edits their own work products in their own
workspace, not through the guide. No LLM: a work item's title/link/context
is a fact from a file or it is honestly absent; there is no generative
fallback here (the hard-fail policy's "no invented facts" applies just as
much to structural data as to card content).

Two levels, matching FORMAT.md's own type axis:
  generic — any base with 2.2-typed entries in type-index.json. Title + link
    only; no status, no ranking (guide-kit has no portable-across-tools
    notion of "active").
  iwe — this specific template's convention: today's DayPlan table (peer
    session 2026-07-16/17, turn 11 — an earlier bottleneck/priority design
    was scrapped: docs/WP-REGISTRY.md's "P" column is a long-lived project
    ID, not urgency, so ranking by it would have grouped by domain instead
    of picking "today"). DayPlan is already a curated, already-ordered "what
    matters today" list — this module reads it as-is, no re-ranking, no cap.

`work_section: off` is the default everywhere (config off-by-default,
peer session escalation 5) — enabling `iwe` for a real installation is a
pilot-only decision made live in chat, not a self-touchable file marker.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import date

logger = logging.getLogger(__name__)

_TYPE_INDEX_REL = os.path.join(".structurer", "type-index.json")

# DayPlan table row: | 🚦 | ТВС | # | РП | h | Статус |
# The traffic-light and ТВС columns are pinned to DayPlan's own documented
# legend (day-open-scaffold.sh output; "> ТВС: В = Важное · Т = Текущее ·
# С = Срочное") rather than "any non-whitespace token" — DayPlan's older
# archived revisions use unrelated 6-column tables elsewhere in the same
# file (changelogs, note-review tables) that would otherwise false-match.
_DAYPLAN_ROW_RE = re.compile(
    r"^\|\s*(?P<traffic>[🔴🟡🟢⚪⚫])\s*\|\s*(?P<tvs>[ВТС])\s*\|\s*(?P<num>[^|]+?)\s*\|"
    r"\s*(?P<title>[^|]+?)\s*\|\s*(?P<hours>[^|]+?)\s*\|\s*(?P<status>[^|]+?)\s*\|\s*$"
)
# The "#" column is a sequential display-order number ("1", "2", …), not the
# WP number — verified against a real DayPlan during the security-gate
# walkthrough, contradicting an earlier (untested-against-real-data)
# assumption that it held the bare WP number. The identity lives in the
# "РП" column instead, as the bold **WP-{N}** prefix (formatting.md: active
# rows are bold, done rows are struck through — see below).
_WP_NUM_RE = re.compile(r"^~*(?:WP-?)?(\d+)~*$", re.IGNORECASE)

# formatting.md: active rows have a **bold** title; done rows are entirely
# struck through (~~...~~), not bold — so a title that isn't bold-wrapped is
# never an active row, regardless of what the status column's wording is
# (observed values include "in_progress"/"pending" for active rows and a
# bare "✅" for done ones — no reliable literal "done" substring to match).


def _load_type_index(base: str) -> dict:
    path = os.path.join(base, _TYPE_INDEX_REL)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        logger.error("malformed type-index at %r: %s — treating as absent", path, e)
        return {}
    if not isinstance(data, dict):
        logger.error("type-index at %r is not a JSON object (got %s) — treating as absent", path, type(data).__name__)
        return {}
    files = data.get("files", {})
    return files if isinstance(files, dict) else {}


def build_generic_section(base: str) -> tuple[str, list[dict]]:
    """Level 1: any 2.2-typed file from type-index.json, title + link, no ranking.

    Returns (markdown_or_empty, decision_log_entries).
    """
    files = _load_type_index(base)
    entries = [path for path, meta in files.items() if meta.get("type") == "2.2"]
    if not entries:
        return "", [{
            "slot": "work_section",
            "value": None,
            "source": "type-index.json",
            "extraction_method": "absent",
            "note": "no 2.2-typed files found",
        }]

    entries.sort()
    lines = ["## Рабочая часть", ""]
    for rel_path in entries:
        title = os.path.splitext(os.path.basename(rel_path))[0]
        lines.append(f"- **{title}** — [{rel_path}]({rel_path})")

    log = [{
        "slot": "work_section",
        "value": rel_path,
        "source": "type-index.json",
        "extraction_method": "direct",
    } for rel_path in entries]
    return "\n".join(lines), log


def _parse_dayplan_rows(text: str) -> list[dict]:
    """Extracts active RP rows from a DayPlan's "План на сегодня" table.

    Count-aware: only rows matching the six-column shape are considered —
    unrelated tables elsewhere in the file (e.g. "Разбор заметок") don't
    match the column count/labels and are silently skipped, not misread.

    A row counts as an active RP row only if its title is bold-wrapped
    **WP-{N}** — the one identity marker formatting.md guarantees for active
    rows. Non-RP rows (e.g. "Саморазвитие") and done rows (struck through,
    not bold) both fail this the same way, so no separate status-word check
    is needed — and none would be reliable: observed status values include
    "in_progress"/"pending" for active rows and a bare "✅" for done ones.
    """
    rows = []
    for line in text.splitlines():
        m = _DAYPLAN_ROW_RE.match(line.strip())
        if not m:
            continue
        status = m.group("status").strip()
        title_raw = m.group("title").strip()
        bold_match = re.match(r"^\*\*(.+?)\*\*\s*(?:—\s*(.*))?$", title_raw)
        if not bold_match:
            continue
        wp_match = _WP_NUM_RE.match(bold_match.group(1).strip())
        if not wp_match:
            continue  # bold title that isn't a WP-{N} marker
        rows.append({
            "wp": wp_match.group(1),
            "title": bold_match.group(1).strip(),
            "context": (bold_match.group(2) or "").strip(),
            "status": status,
        })
    return rows


def _find_wp_file(base: str, wp_num: str) -> str | None:
    """inbox/WP-{N}/WP-{N}.md — a documented, blocking convention of this
    template (CLAUDE.md "Правило inbox: один РП = одна папка"), not a guess.
    Absent on disk → no link, not an invented path."""
    rel = os.path.join("inbox", f"WP-{wp_num}", f"WP-{wp_num}.md")
    return rel if os.path.isfile(os.path.join(base, rel)) else None


_DEFAULT_DAYPLAN_TEMPLATE = os.path.join("current", "DayPlan {date}.md")


def build_iwe_section(base: str, dayplan_path: str | None) -> tuple[str, list[dict]]:
    """Level 2 (IWE convention): today's DayPlan, as-is order, non-done rows only.

    dayplan_path (config's dayplan_path) — a template containing "{date}"
    (substituted with today's ISO date at call time), a fully literal path
    (no "{date}", e.g. for pointing at a fixed test fixture), or unset — in
    which case the default template above is used. "Today's DayPlan" only
    means anything if the date is resolved at call time, not baked into a
    static config value once and left stale after the day rolls over.
    Missing (day not opened yet, or a non-IWE base) → an honest empty section.
    """
    template = dayplan_path or _DEFAULT_DAYPLAN_TEMPLATE
    resolved_path = template.format(date=date.today().isoformat())

    full_path = resolved_path if os.path.isabs(resolved_path) else os.path.join(base, resolved_path)
    if not os.path.isfile(full_path):
        return "", [{
            "slot": "work_section",
            "value": None,
            "source": "dayplan",
            "extraction_method": "absent",
            "note": f"no DayPlan found at {resolved_path}",
        }]

    with open(full_path, encoding="utf-8") as fh:
        text = fh.read()

    rows = _parse_dayplan_rows(text)
    if not rows:
        return "", [{
            "slot": "work_section",
            "value": None,
            "source": resolved_path,
            "extraction_method": "absent",
            "note": "DayPlan found but no non-done RP rows",
        }]

    lines = ["## Рабочая часть", ""]
    log = []
    for row in rows:
        wp_file = _find_wp_file(base, row["wp"])
        link_text = f"РП-{row['wp']}"
        line = f"- **{row['title']}**"
        if row["context"]:
            line += f" — {row['context']}"
        if wp_file:
            line += f" ([{link_text}]({wp_file}))"
        else:
            line += f" ({link_text})"
        lines.append(line)
        log.append({
            "slot": "work_section",
            "value": row["wp"],
            "source": resolved_path,
            "extraction_method": "direct",
            "note": None if wp_file else "no WP context file found by convention",
        })

    return "\n".join(lines), log


def render_work_section(config: dict, base: str) -> tuple[str, list[dict]]:
    """config keys: work_section (off|generic|iwe, default off), dayplan_path.

    Returns (markdown_or_empty, decision_log_entries) — entries are always
    non-empty (even a fully-off/absent case logs why), matching the
    provenance discipline of the rest of decision_log.
    """
    mode = config.get("work_section") or "off"
    if mode == "off":
        return "", []
    if mode == "generic":
        return build_generic_section(base)
    if mode == "iwe":
        return build_iwe_section(base, config.get("dayplan_path"))

    logger.warning("unknown work_section mode %r — treating as off", mode)
    return "", []
