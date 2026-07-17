"""Tests for work_section.py (WP-483 Phase 4). Run: cd generator && pytest"""

import json
import os

import pytest

from work_section import build_generic_section, build_iwe_section, render_work_section


@pytest.fixture
def base(tmp_path):
    return str(tmp_path)


class TestRenderWorkSection:
    def test_off_by_default(self, base):
        markdown, log = render_work_section({}, base)
        assert markdown == ""
        assert log == []

    def test_off_explicit(self, base):
        markdown, log = render_work_section({"work_section": "off"}, base)
        assert markdown == ""
        assert log == []

    def test_unknown_mode_treated_as_off(self, base):
        markdown, log = render_work_section({"work_section": "bogus"}, base)
        assert markdown == ""
        assert log == []


class TestGenericSection:
    def test_no_type_index_is_honest_empty(self, base):
        markdown, log = build_generic_section(base)
        assert markdown == ""
        assert len(log) == 1
        assert log[0]["extraction_method"] == "absent"

    def test_lists_only_2_2_typed_files(self, base):
        os.makedirs(os.path.join(base, ".structurer"))
        index = {
            "schema_version": 1,
            "files": {
                "notes/plan.md": {"type": "2.2"},
                "notes/fact.md": {"type": "2.3"},
                "notes/idea.md": {"type": "2.2"},
            },
        }
        with open(os.path.join(base, ".structurer", "type-index.json"), "w", encoding="utf-8") as fh:
            json.dump(index, fh)

        markdown, log = build_generic_section(base)
        assert "plan" in markdown
        assert "idea" in markdown
        assert "fact" not in markdown
        assert len(log) == 2

    def test_malformed_type_index_is_honest_empty(self, base):
        os.makedirs(os.path.join(base, ".structurer"))
        with open(os.path.join(base, ".structurer", "type-index.json"), "w", encoding="utf-8") as fh:
            fh.write("{not valid json")

        markdown, log = build_generic_section(base)
        assert markdown == ""
        assert log[0]["extraction_method"] == "absent"


_DAYPLAN_HEADER = "# Day Plan: 17 июля 2026\n\n| 🚦 | ТВС | # | РП | h | Статус |\n|----|-----|---|-----|---|--------|\n"


def _write_dayplan(base, rows_text, rel="current/DayPlan 2026-07-17.md"):
    full = os.path.join(base, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as fh:
        fh.write(_DAYPLAN_HEADER + rows_text)
    return rel


class TestIweSection:
    def test_no_dayplan_path_configured_defaults_to_todays_dayplan_and_is_honest_empty(self, base):
        """dayplan_path unset → the default "current/DayPlan {today}.md" template,
        resolved against today's actual date — no such file in a fresh tmp_path,
        so this exercises the honest-empty path, not a hardcoded fixture date."""
        markdown, log = build_iwe_section(base, None)
        assert markdown == ""
        assert "no DayPlan found at current" in log[0]["note"]

    def test_dayplan_file_missing_is_honest_empty(self, base):
        markdown, log = build_iwe_section(base, "current/DayPlan-that-does-not-exist.md")
        assert markdown == ""
        assert "no DayPlan found" in log[0]["note"]

    def test_date_template_is_resolved_against_today(self, base):
        import datetime
        rel = _write_dayplan(base, "| 🟡 | Т | 401 | **GitHub-орги** — Ф6.1 | 3 | in_progress |\n",
                              rel=f"current/DayPlan {datetime.date.today().isoformat()}.md")
        markdown, _ = build_iwe_section(base, "current/DayPlan {date}.md")
        assert "GitHub-орги" in markdown
        assert rel.endswith(f"{datetime.date.today().isoformat()}.md")

    def test_literal_path_without_placeholder_is_used_as_is(self, base):
        rel = _write_dayplan(base, "| 🟡 | Т | 401 | **GitHub-орги** — Ф6.1 | 3 | in_progress |\n",
                              rel="current/fixed-name.md")
        markdown, _ = build_iwe_section(base, rel)
        assert "GitHub-орги" in markdown

    def test_extracts_non_done_rows(self, base):
        rows = (
            "| 🔴 | В | 483 | **guide-kit** — итог прогона; решение пилота | 1 | needs-decision |\n"
            "| 🟡 | Т | ~~476~~ | ~~ЖЦ данных~~ — закрыта | 1 | done |\n"
            "| ⚪ | Т | — | **Саморазвитие** — D-055 | 1 | pending |\n"
        )
        rel = _write_dayplan(base, rows)
        markdown, log = build_iwe_section(base, rel)
        assert "guide-kit" in markdown
        assert "ЖЦ данных" not in markdown  # done row excluded
        assert "Саморазвитие" not in markdown  # non-RP row (# = "—") excluded
        assert len(log) == 1

    def test_done_status_variants_are_all_excluded(self, base):
        """Real DayPlan history (archive/day-plans/) writes "done" as a bare word,
        with a parenthetical, or with a leading checkmark + em-dash detail —
        never just the literal string "done" (found during code review)."""
        rows = (
            "| 🟡 | Т | 101 | **A** — bare | 1 | done |\n"
            "| 🟡 | Т | 102 | **B** — paren | 1 | done (Ф2 решено) |\n"
            "| 🟡 | Т | 103 | **C** — checkmark | 1 | ✅ done — Ф5-Ф6 задеплоено |\n"
            "| 🟡 | Т | 104 | **D** — still active | 1 | in_progress |\n"
        )
        rel = _write_dayplan(base, rows)
        markdown, log = build_iwe_section(base, rel)
        assert "still active" in markdown
        for excluded in ("bare", "paren", "checkmark"):
            assert excluded not in markdown
        assert len(log) == 1
        assert log[0]["value"] == "104"

    def test_context_after_em_dash_is_kept(self, base):
        rows = "| 🟡 | Т | 401 | **GitHub-орги** — Ф6.1 Волна 1: разделение | 3 | in_progress |\n"
        rel = _write_dayplan(base, rows)
        markdown, _ = build_iwe_section(base, rel)
        assert "Ф6.1 Волна 1" in markdown

    def test_links_to_wp_context_file_when_present(self, base):
        os.makedirs(os.path.join(base, "inbox", "WP-401"))
        with open(os.path.join(base, "inbox", "WP-401", "WP-401.md"), "w", encoding="utf-8") as fh:
            fh.write("---\nwp: 401\n---\n")
        rows = "| 🟡 | Т | 401 | **GitHub-орги** — Ф6.1 | 3 | in_progress |\n"
        rel = _write_dayplan(base, rows)
        markdown, log = build_iwe_section(base, rel)
        assert "inbox/WP-401/WP-401.md" in markdown
        assert log[0]["note"] is None

    def test_no_link_when_wp_file_absent(self, base):
        rows = "| 🟡 | Т | 999 | **Несуществующий РП** — контекст | 1 | pending |\n"
        rel = _write_dayplan(base, rows)
        markdown, log = build_iwe_section(base, rel)
        assert "(РП-999)" in markdown
        assert "inbox/WP-999" not in markdown
        assert log[0]["note"] == "no WP context file found by convention"

    def test_absolute_dayplan_path(self, tmp_path):
        base = str(tmp_path / "base")
        os.makedirs(base)
        dayplan_dir = tmp_path / "elsewhere"
        os.makedirs(dayplan_dir)
        dayplan_file = dayplan_dir / "DayPlan.md"
        dayplan_file.write_text(
            _DAYPLAN_HEADER + "| 🟡 | Т | 401 | **GitHub-орги** — Ф6.1 | 3 | in_progress |\n",
            encoding="utf-8",
        )
        markdown, _ = build_iwe_section(base, str(dayplan_file))
        assert "GitHub-орги" in markdown

    def test_unrelated_table_rows_are_not_misread(self, base):
        """A differently-shaped table elsewhere in the file (e.g. "Разбор заметок")
        must not be picked up just because it also uses pipes."""
        rows = (
            "| 🟡 | Т | 401 | **GitHub-орги** — Ф6.1 | 3 | in_progress |\n"
            "\n"
            "| Заметка | Тип | Предложение | ✅ |\n"
            "|---------|-----|-------------|---|\n"
            "| «Мы помогаем...» | идея | Обсуждение | [ ] |\n"
        )
        rel = _write_dayplan(base, rows)
        markdown, log = build_iwe_section(base, rel)
        assert len(log) == 1
        assert "идея" not in markdown
