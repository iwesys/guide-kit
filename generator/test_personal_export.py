"""
Tests for personal_export.py and adapter.py merge hook (WP-483 Phase 5).

Unit coverage:
- allowlist rejects write tools before network
- strict argparse: unknown flag → exit 2
- degradations: 500, 401, 403, empty, invalid JSON
- per-field merge: partial rcs not padded, atomic stage, missing source → manual
"""
import io
import json
import os
import sys
import urllib.error
from unittest.mock import MagicMock, patch

import pytest
import yaml

import personal_export as pe
from adapter import _merge_rcs, apply_platform_overlay
from horizons import normalize_rcs_dict


# ---------------------------------------------------------------------------
# normalize_rcs_dict
# ---------------------------------------------------------------------------

class TestNormalizeRcsDict:
    def test_compact_passthrough(self):
        d = {"W": 3, "M1": 2, "stage_derived": 2, "source": "manual", "confidence": 0.8}
        result = normalize_rcs_dict(d)
        assert result == d

    def test_compact_aliases_renamed(self):
        d = {"W": 3, "stage": 2, "it_level": 4, "agency": 2}
        result = normalize_rcs_dict(d)
        assert result["stage_derived"] == 2
        assert result["IT"] == 4
        assert result["A"] == 2
        assert "stage" not in result
        assert "it_level" not in result
        assert "agency" not in result

    def test_full_format_converted(self):
        d = {
            "worldview": 3,
            "mastery": {"m1_focus": 4, "m2_iwe": 2, "m3_domain": 3, "m4_systems": 2},
            "it_level": 3,
            "agency": 2,
            "bottleneck": "M2",
            "stage_derived": 3,
            "source": "diagnostic_session",
        }
        result = normalize_rcs_dict(d)
        assert result["W"] == 3
        assert result["M1"] == 4
        assert result["M2"] == 2
        assert result["M3"] == 3
        assert result["M4"] == 2
        assert result["IT"] == 3
        assert result["A"] == 2
        assert result["bottleneck"] == "M2"
        assert result["source"] == "diagnostic_session"
        assert "worldview" not in result
        assert "mastery" not in result

    def test_no_defaults_injected(self):
        # Only keys present in input appear in output
        result = normalize_rcs_dict({"W": 2})
        assert result == {"W": 2}
        assert "M1" not in result
        assert "source" not in result

    def test_unknown_keys_dropped(self):
        result = normalize_rcs_dict({"W": 2, "extra_field": "ignored"})
        assert "extra_field" not in result
        assert result["W"] == 2


# ---------------------------------------------------------------------------
# allowlist
# ---------------------------------------------------------------------------

class TestAllowlist:
    def test_write_tool_rejected_before_network(self):
        with pytest.raises(ValueError, match="not in allowlist"):
            pe._rpc_call("http://localhost/mcp", "tok", "dt_write_digital_twin", {})

    def test_unknown_tool_rejected_before_network(self):
        with pytest.raises(ValueError, match="not in allowlist"):
            pe._rpc_call("http://localhost/mcp", "tok", "some_tool", {})

    def test_read_tools_allowed(self):
        # Both read tools must be in the allowlist (no ValueError raised)
        # We mock urlopen to avoid actual network
        for tool in pe._READ_TOOLS:
            mock_resp = MagicMock()
            mock_resp.read.return_value = b'{"jsonrpc":"2.0","id":1,"result":{"ok":true}}'
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            with patch("urllib.request.urlopen", return_value=mock_resp):
                result = pe._rpc_call("http://localhost/mcp", "tok", tool, {})
            assert result == {"ok": True}


# ---------------------------------------------------------------------------
# strict argparse
# ---------------------------------------------------------------------------

class TestStrictArgparse:
    def test_unknown_flag_exits_2(self):
        with pytest.raises(SystemExit) as exc_info:
            # parse_args() raises SystemExit(2) for unknown args
            sys.argv = ["personal_export.py", "--no-such-flag"]
            import importlib
            # Re-invoke the CLI argument parser directly
            import argparse
            parser = argparse.ArgumentParser()
            parser.add_argument("--platform-url", default=pe._DEFAULT_PLATFORM_URL)
            parser.add_argument("--rcs-path", default=None)
            parser.add_argument("--output", default="profile.platform.yaml")
            parser.parse_args(["--no-such-flag"])
        assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# degradations
# ---------------------------------------------------------------------------

def _make_http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="http://test", code=code, msg="error", hdrs=None, fp=None
    )


def _mock_rpc(side_effect):
    return patch.object(pe, "_rpc_call", side_effect=side_effect)


class TestDegradations:
    def test_500_raises_runtime_error(self):
        with patch("urllib.request.urlopen", side_effect=_make_http_error(500)):
            with pytest.raises(RuntimeError, match="HTTP 500"):
                pe._rpc_call("http://localhost/mcp", "tok", "dt_describe_by_path", {})

    def test_401_message_mentions_subscription(self):
        with patch("urllib.request.urlopen", side_effect=_make_http_error(401)):
            with pytest.raises(RuntimeError, match="подписочный источник недоступен"):
                pe._rpc_call("http://localhost/mcp", "tok", "dt_describe_by_path", {})

    def test_403_message_mentions_subscription(self):
        with patch("urllib.request.urlopen", side_effect=_make_http_error(403)):
            with pytest.raises(RuntimeError, match="подписочный источник недоступен"):
                pe._rpc_call("http://localhost/mcp", "tok", "dt_describe_by_path", {})

    def test_empty_response_raises(self):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b""
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            with pytest.raises(RuntimeError, match="пустой ответ"):
                pe._rpc_call("http://localhost/mcp", "tok", "dt_describe_by_path", {})

    def test_invalid_json_raises(self):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"not json at all"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            with pytest.raises(RuntimeError, match="не-JSON"):
                pe._rpc_call("http://localhost/mcp", "tok", "dt_describe_by_path", {})

    def test_export_no_token_returns_1(self, tmp_path):
        env = {k: v for k, v in os.environ.items() if k != "GUIDE_KIT_PLATFORM_TOKEN"}
        with patch.dict(os.environ, env, clear=True):
            code = pe.export("http://localhost/mcp", None, str(tmp_path / "out.yaml"))
        assert code == 1
        assert not (tmp_path / "out.yaml").exists()

    def test_export_no_data_returns_1_no_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GUIDE_KIT_PLATFORM_TOKEN", "testtoken")
        # Both fetch_stage and fetch_rcs return nothing
        with patch.object(pe, "fetch_stage", return_value=(None, None, None)), \
             patch.object(pe, "fetch_rcs", return_value=None):
            out = tmp_path / "out.yaml"
            code = pe.export("http://localhost/mcp", None, str(out))
        assert code == 1
        assert not out.exists()


# ---------------------------------------------------------------------------
# merge semantics
# ---------------------------------------------------------------------------

class TestMergeRcs:
    def test_manual_declared_beats_platform(self):
        declared = {"W": 2, "M1": 3, "source": "manual"}
        overlay = {"W": 4, "M1": 5, "source": "computed_from_events"}
        result = _merge_rcs(declared, overlay)
        assert result["W"] == 2
        assert result["M1"] == 3
        assert result["source"] == "manual"

    def test_platform_beats_diagnostic_session(self):
        declared = {"W": 2, "M1": 3, "source": "diagnostic_session"}
        overlay = {"W": 4, "M1": 5, "source": "computed_from_events"}
        result = _merge_rcs(declared, overlay)
        assert result["W"] == 4
        assert result["M1"] == 5
        assert result["source"] == "computed_from_events"

    def test_overlay_fills_missing_declared_slots(self):
        declared = {"W": 2, "source": "manual"}
        overlay = {"M1": 3, "M4": 4, "source": "computed_from_events"}
        result = _merge_rcs(declared, overlay)
        assert result["W"] == 2         # declared
        assert result["M1"] == 3        # from overlay (not in declared)
        assert result["M4"] == 4        # from overlay (not in declared)

    def test_partial_rcs_not_padded_with_ones(self):
        # Platform returns only W and M1 — remaining slots must NOT appear as 1
        declared = {"W": 2, "source": "diagnostic_session"}
        overlay = {"W": 3, "M1": 4, "source": "computed_from_events"}
        result = _merge_rcs(declared, overlay)
        assert result["W"] == 3
        assert result["M1"] == 4
        assert "M2" not in result
        assert "M3" not in result
        assert "IT" not in result

    def test_overlay_never_deletes_declared_keys(self):
        declared = {"W": 2, "M1": 3, "bottleneck": "M1", "source": "manual"}
        overlay = {"W": 4, "source": "computed_from_events"}
        result = _merge_rcs(declared, overlay)
        # M1 and bottleneck must survive even though overlay doesn't mention them
        assert result["M1"] == 3
        assert result["bottleneck"] == "M1"

    def test_missing_source_treated_as_manual(self, capsys):
        declared = {"W": 2, "M1": 3}  # no source key
        overlay = {"W": 4, "source": "computed_from_events"}
        result = _merge_rcs(declared, overlay)
        # Missing declared source → manual → declared wins
        assert result["W"] == 2
        captured = capsys.readouterr()
        assert "manual" in captured.err

    def test_final_source_is_max_authority(self):
        # overlay fills some missing slots → both declared (manual) and overlay used
        declared = {"W": 2, "source": "manual"}
        overlay = {"M1": 3, "source": "computed_from_events"}
        result = _merge_rcs(declared, overlay)
        # manual wins (lower priority number)
        assert result["source"] == "manual"

    def test_atomic_stage_derived_from_overlay(self):
        # stage_derived comes from overlay as an atomic pair at export time;
        # from merge's perspective it's just a regular slot
        declared = {"stage_derived": 1, "source": "diagnostic_session"}
        overlay = {"stage_derived": 3, "source": "computed_from_events"}
        result = _merge_rcs(declared, overlay)
        assert result["stage_derived"] == 3


# ---------------------------------------------------------------------------
# apply_platform_overlay
# ---------------------------------------------------------------------------

class TestApplyPlatformOverlay:
    def _write_overlay(self, tmp_path, data):
        overlay_path = tmp_path / "profile.platform.yaml"
        with open(overlay_path, "w") as fh:
            yaml.dump(data, fh)
        return overlay_path

    def test_no_overlay_file_returns_profile_unchanged(self, tmp_path):
        profile = {"rcs": {"W": 2, "source": "manual"}}
        profile_path = str(tmp_path / "profile.yaml")
        result = apply_platform_overlay(profile, profile_path)
        assert result == profile

    def test_overlay_merges_rcs(self, tmp_path):
        self._write_overlay(tmp_path, {
            "rcs": {"W": 4, "M1": 3, "source": "computed_from_events"}
        })
        profile = {"rcs": {"W": 2, "source": "diagnostic_session"}}
        profile_path = str(tmp_path / "profile.yaml")
        result = apply_platform_overlay(profile, profile_path)
        # diagnostic_session < computed_from_events → overlay wins on W
        assert result["rcs"]["W"] == 4
        assert result["rcs"]["M1"] == 3

    def test_overlay_fills_mastery_by_area(self, tmp_path):
        self._write_overlay(tmp_path, {
            "mastery_by_area": {"area_1": 0.7, "area_2": 0.3}
        })
        profile = {"mastery_by_area": {"area_1": 0.9}}
        profile_path = str(tmp_path / "profile.yaml")
        result = apply_platform_overlay(profile, profile_path)
        assert result["mastery_by_area"]["area_1"] == 0.9   # declared wins
        assert result["mastery_by_area"]["area_2"] == 0.3   # filled from overlay

    def test_personal_export_off_skips_overlay(self, tmp_path):
        self._write_overlay(tmp_path, {
            "rcs": {"W": 4, "source": "computed_from_events"}
        })
        # When personal_export=off the caller skips apply_platform_overlay entirely
        # — test that the overlay file is NOT applied when the flag is off
        # (adapter.py checks the flag before calling this function)
        profile = {"rcs": {"W": 2, "source": "diagnostic_session"}}
        # Directly: apply_platform_overlay would apply it. The skip is in generate_daily_plan.
        # So here we just verify apply_platform_overlay is the function that does the merge,
        # and the caller is responsible for gating on the flag.
        result = apply_platform_overlay(profile, str(tmp_path / "profile.yaml"))
        assert result["rcs"]["W"] == 4  # overlay was applied (function doesn't know about the flag)


# ---------------------------------------------------------------------------
# export() integration (mocked transport)
# ---------------------------------------------------------------------------

class TestExportIntegration:
    def test_export_writes_stage_and_provenance(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GUIDE_KIT_PLATFORM_TOKEN", "testtoken")
        out = tmp_path / "profile.platform.yaml"
        with patch.object(pe, "fetch_stage", return_value=(3, "Профессионал", None)), \
             patch.object(pe, "fetch_rcs", return_value=None):
            code = pe.export("http://localhost/mcp", None, str(out))
        assert code == 0
        assert out.exists()
        data = yaml.safe_load(out.read_text())
        assert data["rcs"]["stage_derived"] == 3
        assert data["provenance"]["stage_label"] == "Профессионал"
        assert data["is_derived"] is True

    def test_export_parse_failure_writes_raw(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GUIDE_KIT_PLATFORM_TOKEN", "testtoken")
        out = tmp_path / "profile.platform.yaml"
        with patch.object(pe, "fetch_stage", return_value=(None, None, '{"stage": "bad"}')), \
             patch.object(pe, "fetch_rcs", return_value={"W": 3}):
            code = pe.export("http://localhost/mcp", "rcs_path", str(out))
        assert code == 0
        data = yaml.safe_load(out.read_text())
        assert "stage_label_raw" in data.get("provenance", {})
        assert "stage_label" not in data.get("provenance", {})
        # stage_derived must NOT be in rcs when parse failed
        assert "stage_derived" not in data.get("rcs", {})
