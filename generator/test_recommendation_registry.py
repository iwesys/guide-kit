"""
Tests for recommendation_registry.py: raw-v0 hash, lifecycle transitions, and the
CAS races the WP-521 peer session (2026-10-17) flagged -- confirm vs expiry,
idempotent confirm, failed from each blocked stage.
"""

import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from recommendation_registry import (
    EXPIRED,
    FAILED,
    GROUNDING_CHECKED,
    ISSUED,
    OUTCOME_RECOMMENDATION,
    OUTCOME_REFUSED,
    SHOWN,
    RawV0Candidate,
    RecommendationExpired,
    RecommendationFailed,
    RecommendationIssued,
    RecommendationRegistry,
    RequestConflict,
    TransitionRejected,
    compute_content_hash,
)


class TestComputeContentHash:
    def test_deterministic(self):
        assert compute_content_hash("hello world") == compute_content_hash("hello world")

    def test_sha256_prefix(self):
        assert compute_content_hash("x").startswith("sha256:")

    def test_collapses_whitespace(self):
        assert compute_content_hash("a   b\n\nc") == compute_content_hash("a b c")

    def test_nfc_normalizes_unicode(self):
        # "é" as a single precomposed codepoint vs. "e" + a combining acute accent
        # (decomposed form) -- same text, different bytes.
        decomposed = "é"
        precomposed = "é"
        assert compute_content_hash(decomposed) == compute_content_hash(precomposed)

    def test_different_payload_different_hash(self):
        assert compute_content_hash("a") != compute_content_hash("b")


class TestRawV0Candidate:
    def test_to_dict_shape(self):
        candidate = RawV0Candidate(
            source_ref_type="club_post",
            source_ref_ref="post-42",
            selection_basis="bottleneck",
            content_hash=compute_content_hash("content"),
            topic_ref="topic-7",
        )
        d = candidate.to_dict()
        assert d["contract_version"] == "raw-v0"
        assert d["source_ref"] == {"type": "club_post", "ref": "post-42"}
        assert d["topic_ref"] == "topic-7"

    def test_topic_ref_defaults_to_none(self):
        candidate = RawV0Candidate(
            source_ref_type="t", source_ref_ref="r", selection_basis="b", content_hash="sha256:x"
        )
        assert candidate.to_dict()["topic_ref"] is None


@pytest.fixture
def registry():
    reg = RecommendationRegistry(":memory:")
    yield reg
    reg.close()


class TestHappyPath:
    def test_full_lifecycle_emits_issued(self, registry):
        ref = registry.create(raw_ref="raw-1")
        assert registry.state_of(ref) == "created"
        registry.mark_grounding_checked(ref)
        assert registry.state_of(ref) == GROUNDING_CHECKED
        registry.mark_shown(ref)
        assert registry.state_of(ref) == SHOWN
        event = registry.confirm_issued(ref, surface="telegram")
        assert isinstance(event, RecommendationIssued)
        assert event.recommendation_ref == ref
        assert event.raw_ref == "raw-1"
        assert event.surface == "telegram"
        assert registry.state_of(ref) == ISSUED

    def test_confirm_issued_is_idempotent(self, registry):
        ref = registry.create(raw_ref="raw-1")
        registry.mark_grounding_checked(ref)
        registry.mark_shown(ref)
        first = registry.confirm_issued(ref, surface="telegram")
        second = registry.confirm_issued(ref, surface="telegram")
        assert first is not None
        assert second is None  # already issued -- no duplicate event
        assert registry.state_of(ref) == ISSUED


class TestExpiry:
    def test_expire_from_shown(self, registry):
        ref = registry.create(raw_ref="raw-1")
        registry.mark_grounding_checked(ref)
        registry.mark_shown(ref)
        event = registry.expire(ref)
        assert isinstance(event, RecommendationExpired)
        assert registry.state_of(ref) == EXPIRED

    def test_confirm_after_expiry_is_noop(self, registry):
        ref = registry.create(raw_ref="raw-1")
        registry.mark_grounding_checked(ref)
        registry.mark_shown(ref)
        registry.expire(ref)
        result = registry.confirm_issued(ref, surface="telegram")
        assert result is None  # lost the race -- expired wins, no issued event
        assert registry.state_of(ref) == EXPIRED

    def test_expire_after_issued_is_noop(self, registry):
        ref = registry.create(raw_ref="raw-1")
        registry.mark_grounding_checked(ref)
        registry.mark_shown(ref)
        registry.confirm_issued(ref, surface="telegram")
        result = registry.expire(ref)
        assert result is None  # lost the race -- issued wins, no expired event
        assert registry.state_of(ref) == ISSUED


class TestFailure:
    def test_grounding_failure_from_created(self, registry):
        ref = registry.create(raw_ref="raw-1")
        event = registry.mark_failed(ref, stage="grounding", reason="no grounding gate wiring")
        assert isinstance(event, RecommendationFailed)
        assert event.stage == "grounding"
        assert registry.state_of(ref) == FAILED

    def test_render_failure_from_grounding_checked(self, registry):
        ref = registry.create(raw_ref="raw-1")
        registry.mark_grounding_checked(ref)
        event = registry.mark_failed(ref, stage="render", reason="surface timeout")
        assert event.stage == "render"
        assert registry.state_of(ref) == FAILED

    def test_render_failure_rejected_before_grounding_checked(self, registry):
        ref = registry.create(raw_ref="raw-1")
        with pytest.raises(TransitionRejected):
            registry.mark_failed(ref, stage="render")

    def test_unknown_stage_rejected(self, registry):
        ref = registry.create(raw_ref="raw-1")
        with pytest.raises(ValueError):
            registry.mark_failed(ref, stage="bogus")


class TestInvalidTransitions:
    def test_shown_before_grounding_checked_rejected(self, registry):
        ref = registry.create(raw_ref="raw-1")
        with pytest.raises(TransitionRejected):
            registry.mark_shown(ref)

    def test_confirm_before_shown_is_noop(self, registry):
        ref = registry.create(raw_ref="raw-1")
        registry.mark_grounding_checked(ref)
        assert registry.confirm_issued(ref, surface="telegram") is None
        assert registry.state_of(ref) == GROUNDING_CHECKED

    def test_unknown_ref_raises(self, registry):
        with pytest.raises(TransitionRejected):
            registry.state_of("does-not-exist")


class TestConcurrency:
    """Real multi-threaded races against one shared registry instance (review finding High-2)."""

    def test_concurrent_confirm_issued_wins_exactly_once(self, registry):
        ref = registry.create(raw_ref="raw-1")
        registry.mark_grounding_checked(ref)
        registry.mark_shown(ref)

        results = []
        barrier = threading.Barrier(20)

        def attempt():
            barrier.wait()
            results.append(registry.confirm_issued(ref, surface="telegram"))

        threads = [threading.Thread(target=attempt) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        winners = [r for r in results if r is not None]
        assert len(winners) == 1
        assert registry.state_of(ref) == ISSUED

    def test_concurrent_confirm_and_expire_exactly_one_wins(self, registry):
        ref = registry.create(raw_ref="raw-1")
        registry.mark_grounding_checked(ref)
        registry.mark_shown(ref)

        outcomes = []
        barrier = threading.Barrier(2)

        def confirm():
            barrier.wait()
            outcomes.append(("confirm", registry.confirm_issued(ref, surface="telegram")))

        def expire():
            barrier.wait()
            outcomes.append(("expire", registry.expire(ref)))

        threads = [threading.Thread(target=confirm), threading.Thread(target=expire)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        wins = [(kind, event) for kind, event in outcomes if event is not None]
        assert len(wins) == 1
        final_state = registry.state_of(ref)
        assert final_state in (ISSUED, EXPIRED)
        assert wins[0][0] == ("confirm" if final_state == ISSUED else "expire")


# --- server flow: request key, issue history, outbox (WP-521 F12, 2026-11-25) ---

PARAMS = {"case": "c1", "probe": "p1", "date": "2026-11-25", "slot": "slot-a"}


class FakeClock:
    """Mutable UTC clock in the registry's ISO format."""

    def __init__(self, start="2026-11-25T08:00:00Z"):
        self._now = datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)

    def __call__(self):
        return self._now.strftime("%Y-%m-%dT%H:%M:%SZ")

    def advance(self, **kwargs):
        self._now += timedelta(**kwargs)


def _issue(registry, person_id, request_id, method_code, surface="morning-message"):
    """Run one recommendation through the whole lifecycle to a confirmed issue."""
    result = registry.request_recommendation(person_id, request_id, PARAMS, "sha256:x", method_code)
    ref = result.recommendation_ref
    registry.mark_grounding_checked(ref)
    registry.mark_shown(ref)
    return ref, registry.confirm_issued(ref, surface=surface)


class TestRequestIdempotency:
    def test_same_key_and_params_replay_the_same_recommendation(self):
        registry = RecommendationRegistry(":memory:")
        first = registry.request_recommendation("p-1", "req-1", PARAMS, "sha256:x", "LMS.M.026")
        second = registry.request_recommendation("p-1", "req-1", PARAMS, "sha256:x", "LMS.M.026")
        assert first.replayed is False
        assert second.replayed is True
        assert second.recommendation_ref == first.recommendation_ref
        assert second.outcome == OUTCOME_RECOMMENDATION
        assert registry.state_of(first.recommendation_ref) == "created"

    def test_number_is_ascii_rec_uuid_within_40_chars(self):
        registry = RecommendationRegistry(":memory:")
        ref = registry.request_recommendation("p-1", "req-1", PARAMS, "sha256:x", "m").recommendation_ref
        assert ref.startswith("rec_")
        assert len(ref) == 40
        assert ref.isascii()

    def test_same_key_with_different_params_is_a_conflict(self):
        registry = RecommendationRegistry(":memory:")
        registry.request_recommendation("p-1", "req-1", PARAMS, "sha256:x", "m")
        with pytest.raises(RequestConflict):
            registry.request_recommendation("p-1", "req-1", {**PARAMS, "probe": "edited"}, "sha256:x", "m")

    def test_param_key_order_does_not_matter(self):
        registry = RecommendationRegistry(":memory:")
        a = registry.request_recommendation("p-1", "req-1", {"a": 1, "b": 2}, "sha256:x", "m")
        b = registry.request_recommendation("p-1", "req-1", {"b": 2, "a": 1}, "sha256:x", "m")
        assert b.recommendation_ref == a.recommendation_ref

    def test_same_request_id_for_another_person_is_independent(self):
        registry = RecommendationRegistry(":memory:")
        a = registry.request_recommendation("p-1", "req-1", PARAMS, "sha256:x", "m")
        b = registry.request_recommendation("p-2", "req-1", PARAMS, "sha256:x", "m")
        assert a.recommendation_ref != b.recommendation_ref

    def test_refusal_is_stored_and_replayed_without_a_recommendation(self):
        registry = RecommendationRegistry(":memory:")
        first = registry.refuse_request("p-1", "req-2", PARAMS, "no_verifiable_method")
        again = registry.refuse_request("p-1", "req-2", PARAMS, "no_verifiable_method")
        assert (first.outcome, first.recommendation_ref, first.reason_code) == (
            OUTCOME_REFUSED,
            None,
            "no_verifiable_method",
        )
        assert again.replayed is True and again.reason_code == "no_verifiable_method"

    def test_request_after_refusal_with_same_key_is_a_conflict_not_a_stale_refusal(self):
        registry = RecommendationRegistry(":memory:")
        registry.refuse_request("p-1", "req-2", PARAMS, "no_verifiable_method")
        with pytest.raises(RequestConflict):
            registry.request_recommendation("p-1", "req-2", PARAMS, "sha256:x", "m")

    def test_refusal_after_recommendation_with_same_key_is_a_conflict(self):
        registry = RecommendationRegistry(":memory:")
        registry.request_recommendation("p-1", "req-3", PARAMS, "sha256:x", "m")
        with pytest.raises(RequestConflict):
            registry.refuse_request("p-1", "req-3", PARAMS, "no_verifiable_method")

    def test_same_key_with_another_method_or_raw_ref_is_a_conflict(self):
        registry = RecommendationRegistry(":memory:")
        registry.request_recommendation("p-1", "req-4", PARAMS, "sha256:x", "LMS.M.026")
        with pytest.raises(RequestConflict):
            registry.request_recommendation("p-1", "req-4", PARAMS, "sha256:x", "LMS.M.027")
        with pytest.raises(RequestConflict):
            registry.request_recommendation("p-1", "req-4", PARAMS, "sha256:other", "LMS.M.026")

    @pytest.mark.parametrize("bad", [None, "", "method-café"])
    def test_method_and_reason_codes_must_be_non_empty_ascii(self, bad):
        registry = RecommendationRegistry(":memory:")
        with pytest.raises(ValueError):
            registry.request_recommendation("p-1", "req-5", PARAMS, "sha256:x", bad)
        with pytest.raises(ValueError):
            registry.refuse_request("p-1", "req-5", PARAMS, bad)
        assert registry._conn.execute("SELECT COUNT(*) FROM request").fetchone() == (0,)

    def test_concurrent_instances_on_one_database_create_one_recommendation(self, tmp_path):
        db = str(tmp_path / "registry.db")
        refs, errors = [], []

        def worker():
            try:
                registry = RecommendationRegistry(db)
                refs.append(registry.request_recommendation("p-1", "req-1", PARAMS, "sha256:x", "m").recommendation_ref)
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


class TestConfirmIsAtomicWithHistoryAndOutbox:
    def test_confirm_writes_history_and_one_outbox_event(self):
        registry = RecommendationRegistry(":memory:", clock=FakeClock())
        ref, event = _issue(registry, "p-1", "req-1", "LMS.M.026")
        assert event is not None and event.recommendation_ref == ref
        assert registry.methods_issued_since("p-1") == {"LMS.M.026"}
        pending = registry.pending_events()
        assert [e["event_id"] for e in pending] == [f"{ref}:recommendation_issued"]
        assert pending[0]["payload"]["issued_at"] == "2026-11-25T08:00:00Z"

    def test_repeated_confirm_adds_neither_history_nor_event(self):
        registry = RecommendationRegistry(":memory:", clock=FakeClock())
        ref, _ = _issue(registry, "p-1", "req-1", "LMS.M.026")
        assert registry.confirm_issued(ref, surface="morning-message") is None
        assert len(registry.pending_events()) == 1
        assert registry.methods_issued_since("p-1") == {"LMS.M.026"}

    def test_failure_while_recording_leaves_state_shown_and_nothing_written(self, monkeypatch):
        registry = RecommendationRegistry(":memory:", clock=FakeClock())
        ref = registry.request_recommendation("p-1", "req-1", PARAMS, "sha256:x", "m").recommendation_ref
        registry.mark_grounding_checked(ref)
        registry.mark_shown(ref)

        def broken(_event):
            raise RuntimeError("disk full")

        monkeypatch.setattr(registry, "_record_issue", broken)
        with pytest.raises(RuntimeError):
            registry.confirm_issued(ref, surface="morning-message")
        assert registry.state_of(ref) == SHOWN
        assert registry.methods_issued_since("p-1") == set()
        assert registry.pending_events() == []

        monkeypatch.undo()
        assert registry.confirm_issued(ref, surface="morning-message") is not None
        assert registry.state_of(ref) == ISSUED

    def test_ref_made_by_plain_create_gets_no_history(self):
        registry = RecommendationRegistry(":memory:", clock=FakeClock())
        ref = registry.create("sha256:x")
        registry.mark_grounding_checked(ref)
        registry.mark_shown(ref)
        assert registry.confirm_issued(ref, surface="s") is not None
        assert registry.pending_events() == []


class TestSevenDayWindow:
    def test_issued_method_is_listed_inside_the_window_and_dropped_after(self):
        clock = FakeClock()
        registry = RecommendationRegistry(":memory:", clock=clock)
        _issue(registry, "p-1", "req-1", "LMS.M.026")
        clock.advance(days=6)
        assert registry.methods_issued_since("p-1", days=7) == {"LMS.M.026"}
        clock.advance(days=2)
        assert registry.methods_issued_since("p-1", days=7) == set()

    def test_another_persons_history_does_not_count(self):
        registry = RecommendationRegistry(":memory:", clock=FakeClock())
        _issue(registry, "p-1", "req-1", "LMS.M.026")
        assert registry.methods_issued_since("p-2") == set()

    def test_created_and_shown_but_unconfirmed_do_not_count(self):
        registry = RecommendationRegistry(":memory:", clock=FakeClock())
        ref = registry.request_recommendation("p-1", "req-1", PARAMS, "sha256:x", "LMS.M.026").recommendation_ref
        assert registry.methods_issued_since("p-1") == set()
        registry.mark_grounding_checked(ref)
        registry.mark_shown(ref)
        assert registry.methods_issued_since("p-1") == set()

    def test_history_survives_reopening_the_database(self, tmp_path):
        db = str(tmp_path / "registry.db")
        clock = FakeClock()
        first = RecommendationRegistry(db, clock=clock)
        _issue(first, "p-1", "req-1", "LMS.M.026")
        first.close()
        clock.advance(days=1)
        reopened = RecommendationRegistry(db, clock=clock)
        assert reopened.methods_issued_since("p-1") == {"LMS.M.026"}
        assert len(reopened.pending_events()) == 1


class TestOutboxDelivery:
    def test_delivered_event_leaves_the_pending_list_and_redelivery_is_harmless(self):
        clock = FakeClock()
        registry = RecommendationRegistry(":memory:", clock=clock)
        ref, _ = _issue(registry, "p-1", "req-1", "LMS.M.026")
        event_id = f"{ref}:recommendation_issued"
        registry.mark_event_delivered(event_id)
        clock.advance(minutes=5)
        registry.mark_event_delivered(event_id)
        assert registry.pending_events() == []


class TestHardeningFoundByIndependentReview:
    """Edge cases an independent review found uncovered (2026-11-25)."""

    def test_window_boundary_exactly_seven_days_is_inside_one_second_more_is_outside(self):
        clock = FakeClock()
        registry = RecommendationRegistry(":memory:", clock=clock)
        _issue(registry, "p-1", "req-1", "LMS.M.026")
        clock.advance(days=7)
        assert registry.methods_issued_since("p-1", days=7) == {"LMS.M.026"}
        clock.advance(seconds=1)
        assert registry.methods_issued_since("p-1", days=7) == set()

    def test_second_delivery_mark_keeps_the_first_delivery_time(self):
        clock = FakeClock()
        registry = RecommendationRegistry(":memory:", clock=clock)
        ref, _ = _issue(registry, "p-1", "req-1", "LMS.M.026")
        event_id = f"{ref}:recommendation_issued"
        registry.mark_event_delivered(event_id)
        clock.advance(minutes=5)
        registry.mark_event_delivered(event_id)
        delivered_at = registry._conn.execute(
            "SELECT delivered_at FROM event_outbox WHERE event_id = ?", (event_id,)
        ).fetchone()[0]
        assert delivered_at == "2026-11-25T08:00:00Z"

    def test_unknown_event_id_is_rejected(self):
        registry = RecommendationRegistry(":memory:", clock=FakeClock())
        with pytest.raises(ValueError):
            registry.mark_event_delivered("rec_nope:recommendation_issued")

    def test_every_timestamp_comes_from_the_injected_clock(self):
        fixed = "2001-02-03T04:05:06Z"
        registry = RecommendationRegistry(":memory:", clock=lambda: fixed)
        ref, event = _issue(registry, "p-1", "req-1", "LMS.M.026")
        created_at = registry._conn.execute("SELECT created_at FROM request").fetchone()[0]
        registry.mark_event_delivered(f"{ref}:recommendation_issued")
        delivered_at = registry._conn.execute("SELECT delivered_at FROM event_outbox").fetchone()[0]
        second = registry.request_recommendation("p-1", "req-2", PARAMS, "sha256:x", "LMS.M.027").recommendation_ref
        registry.mark_grounding_checked(second)
        registry.mark_shown(second)
        expired = registry.expire(second)
        third = registry.request_recommendation("p-1", "req-3", PARAMS, "sha256:x", "LMS.M.028").recommendation_ref
        failed = registry.mark_failed(third, stage="grounding")
        assert {event.issued_at, created_at, delivered_at, expired.expired_at, failed.failed_at} == {fixed}

    @pytest.mark.parametrize(
        "bad_stamp",
        ["2026-11-25T08:00:00+00:00", "2026-11-25T08:00:00.123Z", "2026-11-25 08:00:00Z"],
    )
    def test_clock_in_another_format_is_rejected_before_anything_is_stored(self, bad_stamp):
        registry = RecommendationRegistry(":memory:", clock=lambda: bad_stamp)
        with pytest.raises(ValueError):
            registry.request_recommendation("p-1", "req-1", PARAMS, "sha256:x", "m")
        assert registry._conn.execute("SELECT COUNT(*) FROM request").fetchone() == (0,)
        assert registry._conn.execute("SELECT COUNT(*) FROM recommendation").fetchone() == (0,)

    def test_failure_on_the_second_write_rolls_back_the_state_and_the_history_row(self):
        registry = RecommendationRegistry(":memory:", clock=FakeClock())
        ref = registry.request_recommendation("p-1", "req-1", PARAMS, "sha256:x", "m").recommendation_ref
        registry.mark_grounding_checked(ref)
        registry.mark_shown(ref)
        # An outbox row with the same event_id makes the SECOND insert fail after the first succeeded.
        registry._conn.execute(
            "INSERT INTO event_outbox (event_id, event_type, recommendation_ref, payload_json, created_at) "
            "VALUES (?, 'recommendation_issued', ?, '{}', '2026-11-25T08:00:00Z')",
            (f"{ref}:recommendation_issued", ref),
        )
        registry._conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            registry.confirm_issued(ref, surface="morning-message")
        assert registry.state_of(ref) == SHOWN
        assert registry._conn.execute("SELECT COUNT(*) FROM issue_history").fetchone() == (0,)
        assert registry._conn.in_transaction is False

    def test_event_payload_names_the_person_and_the_method(self):
        registry = RecommendationRegistry(":memory:", clock=FakeClock())
        _issue(registry, "p-1", "req-1", "LMS.M.026")
        payload = registry.pending_events()[0]["payload"]
        assert payload["person_id"] == "p-1"
        assert payload["method_code"] == "LMS.M.026"

    def test_pending_events_come_oldest_first(self):
        clock = FakeClock()
        registry = RecommendationRegistry(":memory:", clock=clock)
        first, _ = _issue(registry, "p-1", "req-1", "LMS.M.026")
        clock.advance(hours=1)
        second, _ = _issue(registry, "p-1", "req-2", "LMS.M.027")
        assert [e["event_id"] for e in registry.pending_events()] == [
            f"{first}:recommendation_issued",
            f"{second}:recommendation_issued",
        ]

    def test_lost_database_lock_leaves_no_open_transaction_and_does_not_block_others(self, tmp_path):
        db = str(tmp_path / "registry.db")
        registry = RecommendationRegistry(db, clock=FakeClock(), timeout=0.05)
        ref = registry.request_recommendation("p-1", "req-1", PARAMS, "sha256:x", "m").recommendation_ref
        other = sqlite3.connect(db, isolation_level=None)
        other.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError):
            registry.mark_grounding_checked(ref)
        assert registry._conn.in_transaction is False
        other.execute("ROLLBACK")
        other.close()
        # The failed attempt left nothing behind: the same transition now succeeds and a second
        # instance can write at once.
        registry.mark_grounding_checked(ref)
        second = RecommendationRegistry(db, clock=FakeClock(), timeout=0.05)
        second.refuse_request("p-2", "req-1", PARAMS, "no_verifiable_method")

    def test_racing_instances_leave_exactly_one_row_in_each_table(self, tmp_path):
        db = str(tmp_path / "registry.db")
        errors = []

        def worker():
            try:
                registry = RecommendationRegistry(db)
                registry.request_recommendation("p-1", "req-1", PARAMS, "sha256:x", "m")
                registry.close()
            except Exception as exc:  # collected and asserted below, never swallowed
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        check = sqlite3.connect(db)
        assert check.execute("SELECT COUNT(*) FROM recommendation").fetchone() == (1,)
        assert check.execute("SELECT COUNT(*) FROM request").fetchone() == (1,)
        check.close()
