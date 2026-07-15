"""Tests for onboarding_ctas.py (WP-483 Phase 3). Run: cd generator && pytest"""

from onboarding_ctas import render_onboarding_ctas


class TestRenderOnboardingCtas:
    def test_disabled_returns_empty(self):
        assert render_onboarding_ctas({"onboarding_ctas": False}) == ""

    def test_default_is_enabled(self):
        assert render_onboarding_ctas({}) != ""

    def test_enabled_without_url_omits_link_line(self):
        appendix = render_onboarding_ctas({"onboarding_ctas": True})
        assert "Ссылка:" not in appendix
        assert "MCP-серверу платформы" in appendix

    def test_enabled_with_url_includes_link_line(self):
        appendix = render_onboarding_ctas(
            {"onboarding_ctas": True, "platform_connect_url": "https://example.invalid/connect"}
        )
        assert "Ссылка: https://example.invalid/connect" in appendix

    def test_no_cascade_enumeration(self):
        """The CTA text must never list the onboarding cascade as a procedure
        (account → subscription → consent → diagnostics → sensors) — that would
        be a soft copy of the platform's own state machine, the exact anti-pattern
        this phase's consensus forbids."""
        appendix = render_onboarding_ctas({})
        for word in ("аккаунт", "подписк", "согласи", "диагностик", "сенсор"):
            assert word not in appendix.lower()

    def test_includes_iwe_setup_step(self):
        appendix = render_onboarding_ctas({})
        assert "setup.sh" in appendix
        assert "Claude Code" in appendix
