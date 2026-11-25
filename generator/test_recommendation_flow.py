"""
Tests for recommendation_flow.py: method selection from a catalog snapshot, the seven-day window,
replay, delivery confirmation and the command line over a file database.
"""

import json
import threading

import pytest

from recommendation_flow import (
    FLAG_MATERIAL_CHANGED,
    FLAG_MATERIAL_UNAVAILABLE,
    FLAG_NOT_LIVE,
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
    CREATED,
    EXPIRED,
    ISSUED,
    OUTCOME_RECOMMENDATION,
    OUTCOME_REFUSED,
    RecommendationRegistry,
    RequestConflict,
    SHOWN,
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


class TestRaceBetweenIdenticalRequests:
    def test_a_request_that_lost_the_race_gets_the_stored_outcome_not_a_conflict(self, snapshot):
        clock = FakeClock()
        registry = RecommendationRegistry(":memory:", clock=clock)
        flow = RecommendationFlow(registry, snapshot)
        first = flow.request("p-1", "req-1", PARAMS)
        _show_and_confirm(flow, registry, first)  # a later pick would now be another method

        real_replay = registry.replay_request
        calls = []

        def blind_first_time(person_id, request_id, params):
            calls.append(1)
            return None if len(calls) == 1 else real_replay(person_id, request_id, params)

        registry.replay_request = blind_first_time  # the racer looked before the winner stored
        loser = flow.request("p-1", "req-1", PARAMS)
        assert loser.result.replayed is True
        assert loser.result.recommendation_ref == first.result.recommendation_ref
        assert loser.result.method_code == "M.052"

    def test_a_race_with_other_parameters_is_still_a_conflict(self, snapshot):
        registry = RecommendationRegistry(":memory:", clock=FakeClock())
        flow = RecommendationFlow(registry, snapshot)
        flow.request("p-1", "req-1", PARAMS)
        real_replay = registry.replay_request
        calls = []

        def blind_first_time(person_id, request_id, params):
            calls.append(1)
            return None if len(calls) == 1 else real_replay(person_id, request_id, params)

        registry.replay_request = blind_first_time
        with pytest.raises(RequestConflict):
            flow.request("p-1", "req-1", {**PARAMS, "probe": "edited"})

    def test_parallel_instances_on_one_file_all_get_one_recommendation(self, tmp_path, snapshot):
        db = str(tmp_path / "registry.db")
        refs, errors = [], []

        def worker():
            try:
                registry = RecommendationRegistry(db)
                refs.append(RecommendationFlow(registry, snapshot).request("p-1", "req-1", PARAMS).result.recommendation_ref)
                registry.close()
            except Exception as exc:  # collected and asserted below, never swallowed
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        assert len(set(refs)) == 1


class TestReplayHonesty:
    def test_changed_material_is_reported_and_no_texts_are_returned(self, snapshot):
        registry = RecommendationRegistry(":memory:", clock=FakeClock())
        first = RecommendationFlow(registry, snapshot).request("p-1", "req-1", PARAMS)
        changed = dict(snapshot.methods)
        changed["M.052"] = Method("M.052", "core", 0, (), "another next action", "another criterion")
        again = RecommendationFlow(registry, CatalogSnapshot("rev-2", changed)).request("p-1", "req-1", PARAMS)
        assert again.result.recommendation_ref == first.result.recommendation_ref
        assert FLAG_MATERIAL_CHANGED in again.flags
        assert again.method is None

    def test_same_material_under_a_new_catalog_revision_is_not_a_change(self, snapshot):
        registry = RecommendationRegistry(":memory:", clock=FakeClock())
        first = RecommendationFlow(registry, snapshot).request("p-1", "req-1", PARAMS)
        again = RecommendationFlow(registry, CatalogSnapshot("rev-2", snapshot.methods)).request("p-1", "req-1", PARAMS)
        assert FLAG_MATERIAL_CHANGED not in again.flags
        assert again.method is not None and again.method.next_action == first.method.next_action

    def test_raw_ref_depends_on_the_material_texts_not_on_the_revision(self, snapshot):
        def raw_ref_for(snap):
            registry = RecommendationRegistry(":memory:", clock=FakeClock())
            return RecommendationFlow(registry, snap).request("p-1", "req-1", PARAMS).result.raw_ref

        base = raw_ref_for(snapshot)
        assert base.startswith("sha256:") and base == raw_ref_for(snapshot)
        other_text = dict(snapshot.methods)
        other_text["M.052"] = Method("M.052", "core", 0, (), "another next action", "another criterion")
        assert raw_ref_for(CatalogSnapshot("rev-1", other_text)) != base
        assert raw_ref_for(CatalogSnapshot("rev-2", snapshot.methods)) == base

    def test_method_without_material_in_the_new_snapshot_is_flagged_unavailable(self, snapshot):
        registry = RecommendationRegistry(":memory:", clock=FakeClock())
        RecommendationFlow(registry, snapshot).request("p-1", "req-1", PARAMS)
        stripped = dict(snapshot.methods)
        stripped["M.052"] = Method("M.052", "core", 0, (), None, None)
        again = RecommendationFlow(registry, CatalogSnapshot("rev-1", stripped)).request("p-1", "req-1", PARAMS)
        assert FLAG_MATERIAL_UNAVAILABLE in again.flags and again.method is None

    def test_replay_keeps_the_provisional_and_the_slot_flags(self, snapshot):
        flow, _ = _flow(snapshot)
        flow.request("p-1", "req-1", PARAMS)
        again = flow.request("p-1", "req-1", PARAMS)
        assert again.result.replayed is True
        assert FLAG_SELECTION_PROVISIONAL in again.flags and FLAG_SLOT_UNKNOWN in again.flags

    def test_expired_recommendation_replays_with_its_state_and_a_warning_flag(self, snapshot):
        flow, registry = _flow(snapshot)
        first = flow.request("p-1", "req-1", PARAMS)
        ref = first.result.recommendation_ref
        registry.mark_grounding_checked(ref)
        registry.mark_shown(ref)
        registry.expire(ref)
        again = flow.request("p-1", "req-1", PARAMS)
        assert again.state == EXPIRED
        assert FLAG_NOT_LIVE in again.flags

    def test_live_recommendation_has_no_warning_flag(self, snapshot):
        flow, _ = _flow(snapshot)
        outcome = flow.request("p-1", "req-1", PARAMS)
        assert outcome.state == CREATED
        assert FLAG_NOT_LIVE not in outcome.flags


class TestSelectionEdges:
    def test_inside_the_window_a_method_stays_skipped_until_seven_days_pass(self, snapshot):
        clock = FakeClock()
        flow, registry = _flow(snapshot, clock)
        _show_and_confirm(flow, registry, flow.request("p-1", "req-1", PARAMS))
        clock.advance(days=6, hours=23)
        assert flow.request("p-1", "req-2", PARAMS).result.method_code == "M.026"

    def test_same_order_is_broken_by_the_code(self):
        a = Method("M.a", "core", 1, (), "x", "y")
        b = Method("M.b", "core", 1, (), "x", "y")
        snap = CatalogSnapshot("rev", {"M.b": b, "M.a": a})
        assert [m.code for m in eligible_methods(snap, frozenset())] == ["M.a", "M.b"]

    def test_material_needs_both_the_next_action_and_the_criterion(self):
        half = Method("M.h", "core", 0, (), "only the action", None)
        other_half = Method("M.o", "core", 0, (), None, "only the criterion")
        snap = CatalogSnapshot("rev", {"M.h": half, "M.o": other_half})
        assert eligible_methods(snap, frozenset()) == []

    def test_methods_the_person_already_passed_are_skipped_by_the_flow(self, snapshot):
        flow, _ = _flow(snapshot)
        outcome = flow.request("p-1", "req-1", PARAMS, done_codes=frozenset({"M.052"}))
        assert outcome.result.method_code == "M.026"

    def test_the_slot_date_is_part_of_the_key(self, snapshot):
        flow, _ = _flow(snapshot)
        flow.request("p-1", "req-1", PARAMS, slot="slot-a", slot_cut_date="2026-11-20")
        with pytest.raises(RequestConflict):
            flow.request("p-1", "req-1", PARAMS, slot="slot-a", slot_cut_date="2026-11-21")

    def test_slot_keys_inside_params_are_rejected_not_overwritten(self, snapshot):
        flow, _ = _flow(snapshot)
        with pytest.raises(ValueError):
            flow.request("p-1", "req-1", {**PARAMS, "slot": "s1"})

    def test_a_key_of_another_person_is_not_replayed(self, snapshot):
        flow, registry = _flow(snapshot)
        flow.request("p-1", "req-1", PARAMS)
        assert registry.replay_request("p-2", "req-1", {**PARAMS, "slot": None, "slot_cut_date": None}) is None

    def test_a_method_code_with_non_ascii_characters_is_rejected_when_the_snapshot_loads(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text(json.dumps({"source_revision": "r", "methods": [_entry("M.é")]}), encoding="utf-8")
        with pytest.raises(ValueError):
            load_catalog_snapshot(str(path))


class TestShow:
    def test_show_goes_from_created_to_shown_and_is_safe_to_repeat(self, snapshot):
        flow, registry = _flow(snapshot)
        ref = flow.request("p-1", "req-1", PARAMS).result.recommendation_ref
        assert flow.show(ref) == SHOWN
        assert flow.show(ref) == SHOWN
        assert registry.state_of(ref) == SHOWN

    def test_show_resumes_after_a_crash_between_the_two_steps(self, snapshot):
        flow, registry = _flow(snapshot)
        ref = flow.request("p-1", "req-1", PARAMS).result.recommendation_ref
        registry.mark_grounding_checked(ref)  # the process died before mark_shown
        assert flow.show(ref) == SHOWN

    def test_show_after_issue_reports_issued_and_after_expiry_is_rejected(self, snapshot):
        flow, registry = _flow(snapshot)
        issued = flow.request("p-1", "req-1", PARAMS)
        _show_and_confirm(flow, registry, issued)
        assert flow.show(issued.result.recommendation_ref) == ISSUED
        expired = flow.request("p-1", "req-2", PARAMS).result.recommendation_ref
        flow.show(expired)
        registry.expire(expired)
        with pytest.raises(TransitionRejected):
            flow.show(expired)


class TestConfirmDetails:
    def test_surface_and_session_ref_reach_the_event(self, snapshot):
        flow, registry = _flow(snapshot)
        ref = flow.request("p-1", "req-1", PARAMS).result.recommendation_ref
        flow.show(ref)
        confirmation = flow.confirm_delivery(ref, surface="morning-message", session_ref="s-9")
        assert (confirmation.event.surface, confirmation.event.session_ref) == ("morning-message", "s-9")


class TestCommandLine:
    """One process per call, one file database: what the server route does on the real host."""

    def _run(self, capsys, *argv, expect=0):
        assert main(list(argv)) == expect
        return json.loads(capsys.readouterr().out)

    def _request(self, capsys, db, snapshot_file, request_id, *extra, expect=0, params=None):
        return self._run(
            capsys, "--db", db, "request", "--snapshot", snapshot_file, "--person", "p-1",
            "--request-id", request_id, "--params-json", json.dumps(params or PARAMS), *extra, expect=expect,
        )

    def test_full_round_trip_over_separate_calls(self, tmp_path, snapshot_file, capsys):
        db = str(tmp_path / "registry.db")
        base = ["--db", db]
        self._run(capsys, *base, "init")
        issued = self._request(capsys, db, snapshot_file, "req-1", "--slot", "slot-a", "--slot-cut-date", "2026-11-20")
        ref = issued["recommendation_ref"]
        assert ref.startswith("rec_") and len(ref) == 40
        assert issued["method_code"] == "M.052" and issued["state"] == "created"
        assert issued["next_action"] == MATERIAL["next_action"]
        assert issued["criterion"] == MATERIAL["criterion"]
        assert (issued["slot"], issued["slot_cut_date"]) == ("slot-a", "2026-11-20")
        assert issued["flags"] == ["selection_provisional"]

        replay = self._request(capsys, db, snapshot_file, "req-1", "--slot", "slot-a", "--slot-cut-date", "2026-11-20")
        assert replay["replayed"] is True and replay["recommendation_ref"] == ref

        assert self._run(capsys, *base, "show", "--ref", ref)["state"] == "shown"
        assert self._run(capsys, *base, "show", "--ref", ref)["state"] == "shown"
        confirmed = self._run(capsys, *base, "confirm", "--ref", ref, "--surface", "morning-message", "--session-ref", "s-9")
        assert confirmed == {"recommendation_ref": ref, "state": "issued", "new": True}
        again = self._run(capsys, *base, "confirm", "--ref", ref, "--surface", "morning-message")
        assert again["new"] is False

        events = self._run(capsys, *base, "events")["events"]
        assert [e["event_id"] for e in events] == [f"{ref}:recommendation_issued"]
        assert events[0]["payload"]["person_id"] == "p-1" and events[0]["payload"]["method_code"] == "M.052"
        assert events[0]["payload"]["session_ref"] == "s-9"

        self._run(capsys, *base, "delivered", "--event-id", events[0]["event_id"])
        assert self._run(capsys, *base, "events") == {"events": []}

        second = self._request(capsys, db, snapshot_file, "req-2", "--slot", "slot-a", "--slot-cut-date", "2026-11-20")
        assert second["method_code"] == "M.026"  # M.052 was confirmed, the history survived the restarts

    def test_a_refusal_comes_with_its_reason_code(self, tmp_path, capsys):
        path = tmp_path / "umbrellas.json"
        path.write_text(json.dumps({"source_revision": "r", "methods": [_entry("M.010", material=None)]}), encoding="utf-8")
        db = str(tmp_path / "registry.db")
        self._run(capsys, "--db", db, "init")
        refused = self._request(capsys, db, str(path), "req-1")
        assert refused["outcome"] == "refused" and refused["reason_code"] == REASON_NO_METHOD
        assert refused["recommendation_ref"] is None and FLAG_SLOT_UNKNOWN in refused["flags"]

    def test_a_missing_database_is_refused_and_not_created(self, tmp_path, snapshot_file, capsys):
        db = tmp_path / "typo.db"
        for command in (["events"], ["show", "--ref", "x"], ["confirm", "--ref", "x", "--surface", "s"]):
            error = self._run(capsys, "--db", str(db), *command, expect=2)
            assert error["error"] == "database_not_found"
        assert not db.exists()
        self._run(capsys, "--db", str(db), "init")
        assert db.exists()

    def test_failures_print_one_json_object_with_a_stable_code(self, tmp_path, snapshot_file, capsys):
        db = str(tmp_path / "registry.db")
        self._run(capsys, "--db", db, "init")
        issued = self._request(capsys, db, snapshot_file, "req-1")
        assert self._run(
            capsys, "--db", db, "confirm", "--ref", issued["recommendation_ref"], "--surface", "s", expect=2
        )["error"] == "transition_rejected"
        assert self._request(capsys, db, snapshot_file, "req-1", params={**PARAMS, "probe": "edited"}, expect=2)[
            "error"
        ] == "request_conflict"
        assert self._run(
            capsys, "--db", db, "request", "--snapshot", snapshot_file, "--person", "p-1", "--request-id", "r-2",
            "--params-json", "{not json", expect=2,
        )["error"] == "invalid_input"
        assert self._request(capsys, db, snapshot_file, "r-3", params=[1], expect=2)["error"] == "invalid_input"
        assert self._run(capsys, "--db", db, "delivered", "--event-id", "nope", expect=2)["error"] == "invalid_input"

    def test_a_broken_snapshot_or_database_file_is_reported_not_a_traceback(self, tmp_path, capsys):
        db = str(tmp_path / "registry.db")
        self._run(capsys, "--db", db, "init")
        empty = tmp_path / "empty.json"
        empty.write_text("{}", encoding="utf-8")
        assert self._request(capsys, db, str(empty), "req-1", expect=2)["error"] == "invalid_input"
        missing = self._request(capsys, db, str(tmp_path / "none.json"), "req-1", expect=2)
        assert missing["error"] == "invalid_input"
        garbage = tmp_path / "garbage.db"
        garbage.write_bytes(b"this is not a sqlite database at all" * 40)
        assert self._run(capsys, "--db", str(garbage), "events", expect=2)["error"] == "database_error"
