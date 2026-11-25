"""
Tests for recommendation_flow.py: method selection from a catalog snapshot, the seven-day window,
replay, delivery confirmation and the command line over a file database.
"""

import json

import pytest

from recommendation_flow import (
    FLAG_MATERIAL_UNAVAILABLE,
    FLAG_SELECTION_PROVISIONAL,
    FLAG_SLOT_UNKNOWN,
    REASON_ALL_RECENT,
    REASON_NO_METHOD,
    CatalogSnapshot,
    Method,
    RecommendationFlow,
    eligible_methods,
    load_catalog_snapshot,
    main,
)
from recommendation_registry import (
    ISSUED,
    OUTCOME_RECOMMENDATION,
    OUTCOME_REFUSED,
    RecommendationRegistry,
    RequestConflict,
    TransitionRejected,
)
from test_recommendation_registry import FakeClock

PARAMS = {"case": "c1", "probe": "p1", "date": "2026-11-25"}
MATERIAL = {"next_action": "do the next step", "criterion": "it worked or it did not"}


def _entry(code, necessity="core", order=0, prerequisites=(), material=MATERIAL):
    return {
        "code": code,
        "necessity": necessity,
        "order": order,
        "prerequisites": list(prerequisites),
        "material": material,
    }


SNAPSHOT_ENTRIES = [
    _entry("M.026", order=1),
    _entry("M.052", order=0),
    _entry("M.010", material=None),  # umbrella: no material of its own
    _entry("M.043", material=None),
    _entry("M.099", necessity="hygiene"),  # not core
    _entry("M.030", order=2, prerequisites=["M.026"]),  # needs M.026 done first
]


@pytest.fixture
def snapshot_file(tmp_path):
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"source_revision": "rev-1", "methods": SNAPSHOT_ENTRIES}), encoding="utf-8")
    return str(path)


@pytest.fixture
def snapshot(snapshot_file):
    return load_catalog_snapshot(snapshot_file)


def _flow(snapshot, clock=None):
    registry = RecommendationRegistry(":memory:", clock=clock or FakeClock())
    return RecommendationFlow(registry, snapshot), registry


def _show_and_confirm(flow, registry, outcome):
    ref = outcome.result.recommendation_ref
    registry.mark_grounding_checked(ref)
    registry.mark_shown(ref)
    return flow.confirm_delivery(ref, surface="morning-message")


class TestEligibility:
    def test_only_core_methods_with_material_and_met_prerequisites_in_catalog_order(self, snapshot):
        assert [m.code for m in eligible_methods(snapshot, frozenset())] == ["M.052", "M.026"]

    def test_done_method_is_dropped_and_its_dependants_become_eligible(self, snapshot):
        assert [m.code for m in eligible_methods(snapshot, frozenset({"M.026"}))] == ["M.052", "M.030"]

    def test_snapshot_loader_reads_prerequisites_and_material(self, snapshot):
        assert snapshot.source_revision == "rev-1"
        assert snapshot.methods["M.030"].prerequisites == ("M.026",)
        assert snapshot.methods["M.010"].has_material is False
        assert snapshot.methods["M.052"].next_action == MATERIAL["next_action"]


class TestRequest:
    def test_picks_the_first_eligible_method_and_flags_it_provisional(self, snapshot):
        flow, _ = _flow(snapshot)
        outcome = flow.request("p-1", "req-1", PARAMS, slot="slot-a", slot_cut_date="2026-11-20")
        assert outcome.result.outcome == OUTCOME_RECOMMENDATION
        assert outcome.result.method_code == "M.052"
        assert outcome.method.next_action == MATERIAL["next_action"]
        assert (outcome.slot, outcome.slot_cut_date) == ("slot-a", "2026-11-20")
        assert outcome.flags == (FLAG_SELECTION_PROVISIONAL,)

    def test_missing_slot_is_reported_and_does_not_filter(self, snapshot):
        flow, _ = _flow(snapshot)
        outcome = flow.request("p-1", "req-1", PARAMS)
        assert outcome.result.method_code == "M.052"
        assert FLAG_SLOT_UNKNOWN in outcome.flags

    def test_replay_returns_the_stored_recommendation_even_after_history_changed(self, snapshot):
        flow, registry = _flow(snapshot)
        first = flow.request("p-1", "req-1", PARAMS)
        _show_and_confirm(flow, registry, first)  # M.052 is now inside the seven-day window
        again = flow.request("p-1", "req-1", PARAMS)
        assert again.result.replayed is True
        assert again.result.recommendation_ref == first.result.recommendation_ref
        assert again.result.method_code == "M.052"

    def test_same_key_with_other_params_or_other_slot_is_a_conflict(self, snapshot):
        flow, _ = _flow(snapshot)
        flow.request("p-1", "req-1", PARAMS, slot="slot-a")
        with pytest.raises(RequestConflict):
            flow.request("p-1", "req-1", {**PARAMS, "probe": "edited"}, slot="slot-a")
        with pytest.raises(RequestConflict):
            flow.request("p-1", "req-1", PARAMS, slot="slot-b")

    def test_confirmed_methods_leave_the_pool_until_all_are_used_then_a_refusal(self, snapshot):
        flow, registry = _flow(snapshot)
        codes = []
        for index in range(2):
            outcome = flow.request("p-1", f"req-{index}", PARAMS)
            codes.append(outcome.result.method_code)
            _show_and_confirm(flow, registry, outcome)
        assert codes == ["M.052", "M.026"]
        refused = flow.request("p-1", "req-9", PARAMS)
        assert refused.result.outcome == OUTCOME_REFUSED
        assert refused.result.reason_code == REASON_ALL_RECENT
        assert refused.method is None

    def test_another_persons_history_does_not_remove_methods(self, snapshot):
        flow, registry = _flow(snapshot)
        _show_and_confirm(flow, registry, flow.request("p-1", "req-1", PARAMS))
        assert flow.request("p-2", "req-1", PARAMS).result.method_code == "M.052"

    def test_a_method_returns_after_the_window_passes(self, snapshot):
        clock = FakeClock()
        flow, registry = _flow(snapshot, clock)
        _show_and_confirm(flow, registry, flow.request("p-1", "req-1", PARAMS))
        clock.advance(days=8)
        assert flow.request("p-1", "req-2", PARAMS).result.method_code == "M.052"

    def test_created_but_unconfirmed_recommendation_does_not_use_up_a_method(self, snapshot):
        flow, _ = _flow(snapshot)
        flow.request("p-1", "req-1", PARAMS)
        assert flow.request("p-1", "req-2", PARAMS).result.method_code == "M.052"

    def test_no_eligible_method_at_all_is_a_refusal_with_its_own_code(self):
        only_umbrellas = CatalogSnapshot(
            "rev-2",
            {"M.010": Method("M.010", "core", 0, (), None, None)},
        )
        flow, _ = _flow(only_umbrellas)
        outcome = flow.request("p-1", "req-1", PARAMS)
        assert (outcome.result.outcome, outcome.result.reason_code) == (OUTCOME_REFUSED, REASON_NO_METHOD)

    def test_replay_after_the_method_left_the_snapshot_flags_the_material(self, snapshot):
        flow, registry = _flow(snapshot)
        first = flow.request("p-1", "req-1", PARAMS)
        trimmed = RecommendationFlow(registry, CatalogSnapshot("rev-3", {}))
        again = trimmed.request("p-1", "req-1", PARAMS)
        assert again.result.recommendation_ref == first.result.recommendation_ref
        assert FLAG_MATERIAL_UNAVAILABLE in again.flags


class TestConfirmDelivery:
    def test_confirmation_before_the_show_is_rejected_and_records_nothing(self, snapshot):
        flow, registry = _flow(snapshot)
        outcome = flow.request("p-1", "req-1", PARAMS)
        with pytest.raises(TransitionRejected):
            flow.confirm_delivery(outcome.result.recommendation_ref, surface="morning-message")
        assert registry.methods_issued_since("p-1") == set()
        assert registry.pending_events() == []

    def test_confirmation_after_the_show_issues_once_and_a_repeat_is_harmless(self, snapshot):
        flow, registry = _flow(snapshot)
        outcome = flow.request("p-1", "req-1", PARAMS)
        first = _show_and_confirm(flow, registry, outcome)
        second = flow.confirm_delivery(outcome.result.recommendation_ref, surface="morning-message")
        assert first.event is not None
        assert second.event is None
        assert registry.state_of(outcome.result.recommendation_ref) == ISSUED
        assert len(registry.pending_events()) == 1

    def test_confirmation_of_an_expired_recommendation_is_rejected(self, snapshot):
        flow, registry = _flow(snapshot)
        outcome = flow.request("p-1", "req-1", PARAMS)
        ref = outcome.result.recommendation_ref
        registry.mark_grounding_checked(ref)
        registry.mark_shown(ref)
        registry.expire(ref)
        with pytest.raises(TransitionRejected):
            flow.confirm_delivery(ref, surface="morning-message")


class TestCommandLine:
    """One process per call, one file database: what the server route does on the real host."""

    def _run(self, capsys, *argv):
        assert main(list(argv)) == 0
        return json.loads(capsys.readouterr().out)

    def test_request_show_confirm_events_delivered_round_trip(self, tmp_path, snapshot_file, capsys):
        db = str(tmp_path / "registry.db")
        base = ["--db", db]
        issued = self._run(
            capsys, *base, "request", "--snapshot", snapshot_file, "--person", "p-1",
            "--request-id", "req-1", "--params-json", json.dumps(PARAMS), "--slot", "slot-a",
        )
        ref = issued["recommendation_ref"]
        assert ref.startswith("rec_") and len(ref) == 40
        assert issued["method_code"] == "M.052"
        assert issued["next_action"] == MATERIAL["next_action"]

        replay = self._run(
            capsys, *base, "request", "--snapshot", snapshot_file, "--person", "p-1",
            "--request-id", "req-1", "--params-json", json.dumps(PARAMS), "--slot", "slot-a",
        )
        assert replay["replayed"] is True and replay["recommendation_ref"] == ref

        assert self._run(capsys, *base, "show", "--ref", ref)["state"] == "shown"
        confirmed = self._run(capsys, *base, "confirm", "--ref", ref, "--surface", "morning-message")
        assert confirmed == {"recommendation_ref": ref, "state": "issued", "new": True}

        events = self._run(capsys, *base, "events")
        assert [e["event_id"] for e in events] == [f"{ref}:recommendation_issued"]
        assert events[0]["payload"]["person_id"] == "p-1"
        assert events[0]["payload"]["method_code"] == "M.052"

        self._run(capsys, *base, "delivered", "--event-id", events[0]["event_id"])
        assert self._run(capsys, *base, "events") == []

        next_one = self._run(
            capsys, *base, "request", "--snapshot", snapshot_file, "--person", "p-1",
            "--request-id", "req-2", "--params-json", json.dumps(PARAMS), "--slot", "slot-a",
        )
        assert next_one["method_code"] == "M.026"  # M.052 was confirmed, history survived the restarts

    def test_confirm_without_show_fails_loudly(self, tmp_path, snapshot_file, capsys):
        db = str(tmp_path / "registry.db")
        issued = self._run(
            capsys, "--db", db, "request", "--snapshot", snapshot_file, "--person", "p-1",
            "--request-id", "req-1", "--params-json", json.dumps(PARAMS),
        )
        with pytest.raises(TransitionRejected):
            main(["--db", db, "confirm", "--ref", issued["recommendation_ref"], "--surface", "morning-message"])
