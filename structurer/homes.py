"""
homes.py — guide-kit structurer: homes.yaml loader + placement-based typing (FORMAT.md §2).

Segment-based glob matching, not fnmatch.translate on the full path: "*" matches exactly
one path segment, "**" matches zero or more segments — gitignore-style semantics, not
fnmatch's (where "*" crosses "/"). FORMAT.md's specificity precedence rule is itself
flagged "proposed, not backed by an existing convention" (peer-session finding,
2026-07-14) — the tuple scoring below is this implementation's concrete choice for that
open flag, not a second independent decision.
"""
from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass

import yaml

logger = logging.getLogger(__name__)


@dataclass
class HomeRule:
    pattern: str
    type: str
    note: str = ""


def load_homes(path: str) -> list[HomeRule]:
    """Tolerant of a missing file: no homes.yaml means every path falls through to the
    per-file classifier (FORMAT.md §3)."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        logger.info("no homes.yaml at %r — every path falls through to the classifier", path)
        return []
    except yaml.YAMLError as e:
        logger.error("malformed homes.yaml at %r: %s — treating as absent", path, e)
        return []
    return [HomeRule(r["path"], r["type"], r.get("note", "")) for r in data.get("homes", [])]


def _segments(path: str) -> list[str]:
    """Splits on "/" and collapses consecutive "**" into one — "a/**/**/b" means the
    same as "a/**/b", and matching each "**" separately is a polynomial-time trap for
    no semantic gain (a pattern with several would re-scan the remaining path once per
    "**" before the next one collapses)."""
    raw = [s for s in path.split("/") if s != ""]
    collapsed: list[str] = []
    for segment in raw:
        if segment == "**" and collapsed and collapsed[-1] == "**":
            continue
        collapsed.append(segment)
    return collapsed


def _segment_matches(rel_segments: list[str], pattern_segments: list[str]) -> bool:
    """"**" (only valid as a whole segment) consumes zero or more remaining segments;
    every other pattern segment matches exactly one segment via single-segment fnmatch
    (no "/" left inside a segment, so "*"/"?"/"[...]" can't accidentally cross a boundary)."""
    if not pattern_segments:
        return not rel_segments
    head, rest = pattern_segments[0], pattern_segments[1:]
    if head == "**":
        if not rest:
            return True
        return any(_segment_matches(rel_segments[i:], rest) for i in range(len(rel_segments) + 1))
    if not rel_segments:
        return False
    return fnmatch.fnmatch(rel_segments[0], head) and _segment_matches(rel_segments[1:], rest)


def _specificity(pattern_segments: list[str]) -> tuple[int, int]:
    """(literal segments before the first wildcard segment, total literal chars in
    them). No metric is perfect here (e.g. "a/**" vs "**/b.md" tie on "a/b.md" under
    any reasonable scoring) — this is a documented, not a proven-optimal, choice."""
    literal_count = 0
    literal_chars = 0
    for segment in pattern_segments:
        if any(c in segment for c in "*?["):
            break
        literal_count += 1
        literal_chars += len(segment)
    return (literal_count, literal_chars)


def match_home(rel_path: str, rules: list[HomeRule]) -> HomeRule | None:
    """Most specific matching rule wins; ties keep the first match in file order."""
    rel_segments = _segments(rel_path)
    best: HomeRule | None = None
    best_score: tuple[int, int] | None = None
    for rule in rules:
        pattern_segments = _segments(rule.pattern)
        if not _segment_matches(rel_segments, pattern_segments):
            continue
        score = _specificity(pattern_segments)
        if best_score is None or score > best_score:
            best, best_score = rule, score
    return best
