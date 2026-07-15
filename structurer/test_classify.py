"""
test_classify.py — guide-kit structurer tests (WP-483 Ф2).
Run: cd structurer && pytest
"""
from __future__ import annotations

import json

from classify import classify_file, walk_and_classify, write_type_index
from homes import HomeRule, load_homes, match_home
from signals import detect_event_date, read_frontmatter, read_frontmatter_override


# ---------------------------------------------------------------------------
# homes.py — segment matching + specificity scoring
# ---------------------------------------------------------------------------

def test_double_star_matches_nested_path():
    rules = [HomeRule("daily-notes/**", "2.2")]
    assert match_home("daily-notes/2026/note.md", rules).type == "2.2"


def test_single_star_does_not_cross_slash():
    rules = [HomeRule("daily-notes/*", "2.2")]
    assert match_home("daily-notes/note.md", rules) is not None
    assert match_home("daily-notes/2026/note.md", rules) is None


def test_more_specific_pattern_wins():
    rules = [
        HomeRule("daily-notes/**", "2.2"),
        HomeRule("daily-notes/2026/**", "2.3"),
    ]
    assert match_home("daily-notes/2026/note.md", rules).type == "2.3"


def test_more_specific_prefix_beats_root_wildcard():
    # "daily-notes/**" (1 literal segment before its wildcard) outranks
    # "**/archive.md" (0 literal segments before its wildcard) — not a tie.
    rules = [
        HomeRule("**/archive.md", "2.4"),
        HomeRule("daily-notes/**", "2.2"),
    ]
    assert match_home("daily-notes/archive.md", rules).type == "2.2"


def test_tie_keeps_first_rule_in_file_order():
    # Both patterns have identical specificity (1 literal segment, "notes", 5 chars,
    # before their respective wildcard segment) and both match "notes/foo.md" —
    # a genuine tie, broken by file order.
    rules = [
        HomeRule("notes/*.md", "2.4"),
        HomeRule("notes/**", "2.2"),
    ]
    assert match_home("notes/foo.md", rules).type == "2.4"


def test_consecutive_double_star_matches():
    # "a/**/**/b" must behave exactly like "a/**/b".
    rules = [HomeRule("a/**/**/b.md", "2.3")]
    assert match_home("a/x/y/z/b.md", rules).type == "2.3"


def test_no_matching_rule_returns_none():
    rules = [HomeRule("daily-notes/**", "2.2")]
    assert match_home("concepts/idea.md", rules) is None


def test_non_adjacent_double_star_does_not_blow_up():
    # Naive backtracking on several non-adjacent "**" segments is exponential
    # (verified: 12 alternating "**"/"*" segments took 12s unmemoized). The
    # memoized matcher must stay fast regardless — this is the actual regression
    # this test guards, not just the narrow "consecutive **" case above.
    pattern = "/".join(["**", "*"] * 12) + "/target.md"
    rules = [HomeRule(pattern, "2.3")]
    non_matching_path = "/".join(["seg"] * 24) + "/not-the-target.md"
    assert match_home(non_matching_path, rules) is None  # must return promptly, not hang


def test_load_homes_skips_malformed_rule_not_fatal(tmp_path):
    # A rule missing "type" must not abort parsing every other rule in the file.
    f = tmp_path / "homes.yaml"
    f.write_text("homes:\n  - path: broken/**\n  - path: ok/**\n    type: \"2.3\"\n")
    rules = load_homes(str(f))
    assert [r.pattern for r in rules] == ["ok/**"]


def test_load_homes_skips_unrecognized_type(tmp_path):
    f = tmp_path / "homes.yaml"
    f.write_text('homes:\n  - path: "weird/**"\n    type: "2.99"\n')
    assert load_homes(str(f)) == []


def test_load_homes_accepts_auto(tmp_path):
    f = tmp_path / "homes.yaml"
    f.write_text('homes:\n  - path: "archive/**"\n    type: "auto"\n')
    rules = load_homes(str(f))
    assert rules[0].type == "auto"


# ---------------------------------------------------------------------------
# signals.py — frontmatter override + event-date
# ---------------------------------------------------------------------------

def test_frontmatter_override_accepted(tmp_path):
    f = tmp_path / "note.md"
    f.write_text("---\ntype: 2.3\n---\nbody\n")
    fm = read_frontmatter(str(f))
    assert read_frontmatter_override(fm, str(f)) == "2.3"


def test_frontmatter_that_is_not_a_mapping_does_not_crash(tmp_path):
    # "---\n- a\n- b\n---" is syntactically valid YAML (a list), but not a valid
    # frontmatter block — must degrade to "no frontmatter", not raise.
    f = tmp_path / "note.md"
    f.write_text("---\n- a\n- b\n---\nbody\n")
    assert read_frontmatter(str(f)) == {}


def test_frontmatter_override_unknown_value_falls_through(tmp_path):
    f = tmp_path / "note.md"
    f.write_text("---\ntype: not-a-real-type\n---\nbody\n")
    fm = read_frontmatter(str(f))
    assert read_frontmatter_override(fm, str(f)) is None


def test_event_date_from_frontmatter():
    assert detect_event_date("note.md", {"event_date": "2026-06-01"}) is True


def test_bare_date_key_is_not_a_signal():
    assert detect_event_date("note.md", {"date": "2026-06-01"}) is False


def test_event_date_from_filename():
    assert detect_event_date("daily-notes/2026-06-01.md", {}) is True


def test_invalid_filename_date_is_not_a_signal():
    assert detect_event_date("weird/1234-99-99.md", {}) is False


def test_no_signal_at_all():
    assert detect_event_date("concepts/idea.md", {}) is False


# ---------------------------------------------------------------------------
# classify.py — end-to-end precedence + walk
# ---------------------------------------------------------------------------

def test_homes_takes_precedence_over_event_date_signal(tmp_path):
    (tmp_path / "daily-notes").mkdir()
    f = tmp_path / "daily-notes" / "2026-06-01.md"
    f.write_text("body\n")
    homes = [HomeRule("daily-notes/**", "2.3")]
    entry = classify_file("daily-notes/2026-06-01.md", str(f), homes)
    assert entry["type"] == "2.3"
    assert entry["source"] == "homes"


def test_homes_auto_falls_through_to_classifier(tmp_path):
    (tmp_path / "archive").mkdir()
    f = tmp_path / "archive" / "2026-06-01.md"
    f.write_text("body\n")
    homes = [HomeRule("archive/**", "auto")]
    entry = classify_file("archive/2026-06-01.md", str(f), homes)
    assert entry["type"] == "2.2"
    assert entry["source"] == "classifier"


def test_homes_takes_precedence_over_frontmatter_override(tmp_path):
    f = tmp_path / "2026-06-01.md"
    f.write_text("---\ntype: 2.3\n---\nbody\n")
    homes = [HomeRule("*.md", "2.2")]
    entry = classify_file("2026-06-01.md", str(f), homes)
    assert entry["type"] == "2.2"
    assert entry["source"] == "homes"


def test_frontmatter_override_takes_precedence_over_event_date(tmp_path):
    f = tmp_path / "2026-06-01.md"
    f.write_text("---\ntype: 2.3\n---\nbody\n")
    entry = classify_file("2026-06-01.md", str(f), [])
    assert entry["type"] == "2.3"
    assert entry["source"] == "frontmatter"


def test_default_is_2_4_not_a_guess(tmp_path):
    f = tmp_path / "musings.md"
    f.write_text("just some thoughts, no signal at all\n")
    entry = classify_file("musings.md", str(f), [])
    assert entry["type"] == "2.4"
    assert entry["confidence"] == 0.0
    assert entry["source"] == "default"


def test_non_text_file_is_pending_not_skipped(tmp_path):
    f = tmp_path / "photo.png"
    f.write_bytes(b"\x89PNG\r\n")
    entry = classify_file("photo.png", str(f), [])
    assert entry == {"type": None, "pending": "needs-extractor"}


def test_non_text_file_under_homes_rule_gets_type_but_stays_pending(tmp_path):
    # FORMAT.md §4's own whiteboard.png example: homes.yaml can categorize a binary
    # file by folder even with no extractor installed — it assigns a type, it doesn't
    # manufacture text content that isn't there.
    (tmp_path / "photos").mkdir()
    f = tmp_path / "photos" / "whiteboard.png"
    f.write_bytes(b"\x89PNG\r\n")
    homes = [HomeRule("photos/**", "2.3")]
    entry = classify_file("photos/whiteboard.png", str(f), homes)
    assert entry == {"type": "2.3", "pending": "needs-extractor", "source": "homes"}


def test_walk_skips_structurer_output_dir(tmp_path):
    (tmp_path / "note.md").write_text("body\n")
    structurer_dir = tmp_path / ".structurer"
    structurer_dir.mkdir()
    (structurer_dir / "type-index.json").write_text("{}")
    files = walk_and_classify(str(tmp_path), [])
    assert "note.md" in files
    assert not any(p.startswith(".structurer") for p in files)


def test_walk_skips_own_config_files(tmp_path):
    (tmp_path / "note.md").write_text("body\n")
    (tmp_path / "homes.yaml").write_text("homes: []\n")
    files = walk_and_classify(str(tmp_path), [])
    assert "note.md" in files
    assert "homes.yaml" not in files


def test_write_type_index_wraps_with_schema_version(tmp_path):
    out = tmp_path / ".structurer" / "type-index.json"
    write_type_index({"a.md": {"type": "2.4"}}, str(out))
    written = json.loads(out.read_text())
    assert written["schema_version"] == 1
    assert written["files"]["a.md"]["type"] == "2.4"
