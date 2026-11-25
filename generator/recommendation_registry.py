"""
Recommendation registry: raw-v0 contract, delivery events, and the recommendation
lifecycle state machine.

Consensus from the WP-521 peer session (Claude + Kimi + Codex, 2026-10-17):
contract_version `raw-v0` was fixed in an earlier session this same day; this module
is the F14 integration layer that session deferred -- the state machine owner for
a recommendation's path from candidate to a confirmed, idempotent delivery event.

Scope deliberately excludes a candidate_builder (mapping existing selection output
into RawV0Candidate): the peer session found no verified field-provenance mapping
from the current selection code (planner.py / source_finder.py) to raw-v0's five
fields, and blocked that work on a separate provenance audit. This module covers
the two components the session did NOT block: the registry state machine and the
event emitter.

Server flow contract (WP-521 F12, peer session 2026-11-25, Claude + Kimi + Codex)
----------------------------------------------------------------------------------
Request (caller: the server flow acting for the Visionary). Idempotency key is
(person_id, request_id); the same pair always returns the same stored outcome:

    request_recommendation("p-1", "req-1",
        {"case": "...", "probe": "...", "date": "2026-11-25",
         "slot": "...", "slot_cut_date": "2026-11-20"},   # slot read from diagnostics
        raw_ref="sha256:...", method_code="LMS.M.026")
    -> RequestResult(outcome="recommendation", recommendation_ref="rec_<uuid4>", ...)

    refuse_request("p-1", "req-2", {...}, reason_code="no_verifiable_method")
    -> RequestResult(outcome="refused", reason_code="no_verifiable_method", ...)

Same key with different parameters raises RequestConflict, never silently replays.
A new calculation after data appeared must use a new request_id. Reason codes are
ASCII codes; the consumer renders text in its own language.

Confirmation (caller: whoever observed the delivery to the person):

    create is not delivery. mark_grounding_checked -> mark_shown -> confirm_issued.
    confirm_issued is idempotent and, in ONE transaction, moves shown -> issued,
    appends the issue history row and the `recommendation_issued` outbox row.
    The event is delivered later by the consumer from pending_events(); redelivery
    keeps one event_id (`<recommendation_ref>:recommendation_issued`), so a consumer
    dedups on it. One logical event is guaranteed, one physical send is not.

Seven-day window: methods_issued_since(person_id, days=7) lists the method codes
CONFIRMED as issued to that person; created or shown-only recommendations do not count.
"""

import hashlib
import json
import re
import sqlite3
import threading
import unicodedata
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

RAW_V0_VERSION = "raw-v0"

CREATED = "created"
GROUNDING_CHECKED = "grounding_checked"
SHOWN = "shown"
ISSUED = "issued"
EXPIRED = "expired"
FAILED = "failed"


def compute_content_hash(payload: str) -> str:
    """
    Canonical raw-v0.content_hash: NFC-normalize, collapse whitespace, hash the
    result before any metadata (contract_version, source_ref, ...) is attached.
    Rule fixed by the peer session this same day, reused here unchanged.
    """
    normalized = unicodedata.normalize("NFC", payload)
    collapsed = re.sub(r"\s+", " ", normalized).strip()
    digest = hashlib.sha256(collapsed.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


@dataclass
class RawV0Candidate:
    """A recommendation candidate in the raw-v0 contract shape."""

    source_ref_type: str
    source_ref_ref: str
    selection_basis: str
    content_hash: str
    topic_ref: Optional[str] = None
    contract_version: str = RAW_V0_VERSION

    def to_dict(self) -> dict:
        return {
            "contract_version": self.contract_version,
            "source_ref": {"type": self.source_ref_type, "ref": self.source_ref_ref},
            "selection_basis": self.selection_basis,
            "topic_ref": self.topic_ref,
            "content_hash": self.content_hash,
        }


@dataclass
class RecommendationIssued:
    """Thin pointer event: the recommendation was delivered and delivery was confirmed."""

    recommendation_ref: str
    raw_ref: str
    surface: str
    issued_at: str
    session_ref: Optional[str] = None


@dataclass
class RecommendationExpired:
    """The recommendation was shown but delivery was never confirmed before its TTL."""

    recommendation_ref: str
    raw_ref: str
    expired_at: str


@dataclass
class RecommendationFailed:
    """Grounding or rendering failed before the recommendation reached the user."""

    recommendation_ref: str
    raw_ref: str
    stage: str  # "grounding" | "render"
    failed_at: str
    reason: str = ""


class TransitionRejected(Exception):
    """A state transition's precondition did not hold: unknown ref or wrong starting state."""


class RequestConflict(Exception):
    """The same (person_id, request_id) arrived again with different parameters."""


OUTCOME_RECOMMENDATION = "recommendation"
OUTCOME_REFUSED = "refused"

EVENT_RECOMMENDATION_ISSUED = "recommendation_issued"

_ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


@dataclass
class RequestResult:
    """Stored outcome of one request; `replayed` is True when it was already stored."""

    person_id: str
    request_id: str
    outcome: str  # OUTCOME_RECOMMENDATION | OUTCOME_REFUSED
    recommendation_ref: Optional[str]
    method_code: Optional[str]
    reason_code: Optional[str]
    replayed: bool


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime(_ISO_FORMAT)


def _canonical_json(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _require_ascii_code(name: str, value: Optional[str]) -> None:
    if not isinstance(value, str) or not value or not value.isascii():
        raise ValueError(f"{name} must be a non-empty ASCII string, got {value!r}")


class RecommendationRegistry:
    """
    Reference implementation of the recommendation lifecycle:
    created -> grounding_checked -> shown -> issued | expired,
    with failed reachable from created (grounding stage) or grounding_checked
    (render stage).

    Backed by SQLite so the CAS transitions below are real `UPDATE ... WHERE
    state = <expected>` statements, not just in-process locking -- the peer
    session's point was that atomicity must come from checking the row's
    current state, not merely wrapping the write in a transaction. Where this
    registry physically lives in production (this SQLite file vs. the
    platform's own database) was explicitly left to a separate infra check by
    that session; swap the storage by replacing this class while keeping its
    method contract (CAS semantics, event return values).

    `db_path` has no default on purpose: callers must say explicitly where the
    registry lives. A silent `:memory:` default would let two instances (e.g.
    one per worker thread) end up with two disconnected databases that never
    see each other's writes -- confirm_issued's dedup would then silently stop
    working, not fail loudly. Tests pass ":memory:" explicitly.

    Thread-safe for use from multiple threads against one instance: each
    public method serializes through `self._lock`, and the connection is
    opened with `check_same_thread=False` to allow that.
    """

    def __init__(self, db_path: str, clock: Callable[[], str] = _utc_now_iso, timeout: float = 5.0):
        self._lock = threading.Lock()
        self._clock = clock  # UTC ISO-8601 with "Z"; tests inject a fixed time
        self._conn = sqlite3.connect(db_path, timeout=timeout, check_same_thread=False)
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS recommendation (
                recommendation_ref TEXT PRIMARY KEY,
                raw_ref TEXT NOT NULL,
                state TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS request (
                person_id TEXT NOT NULL,
                request_id TEXT NOT NULL,
                request_json TEXT NOT NULL,
                outcome TEXT NOT NULL,
                recommendation_ref TEXT UNIQUE,
                method_code TEXT,
                reason_code TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (person_id, request_id)
            );
            CREATE TABLE IF NOT EXISTS issue_history (
                recommendation_ref TEXT PRIMARY KEY,
                person_id TEXT NOT NULL,
                method_code TEXT NOT NULL,
                issued_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS issue_history_person_time
                ON issue_history (person_id, issued_at);
            CREATE TABLE IF NOT EXISTS event_outbox (
                event_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                recommendation_ref TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                delivered_at TEXT
            );
            """
        )
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _now(self) -> str:
        """The injected clock's time; any shape other than YYYY-MM-DDTHH:MM:SSZ is rejected, not stored."""
        stamp = self._clock()
        datetime.strptime(stamp, _ISO_FORMAT)  # raises ValueError for "+00:00", fractions, spaces
        return stamp

    def create(self, raw_ref: str) -> str:
        """Start a new recommendation attempt in state `created`. Returns its recommendation_ref."""
        recommendation_ref = str(uuid.uuid4())
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO recommendation (recommendation_ref, raw_ref, state) VALUES (?, ?, ?)",
                    (recommendation_ref, raw_ref, CREATED),
                )
        return recommendation_ref

    def _row(self, recommendation_ref: str) -> tuple:
        row = self._conn.execute(
            "SELECT raw_ref, state FROM recommendation WHERE recommendation_ref = ?",
            (recommendation_ref,),
        ).fetchone()
        if row is None:
            raise TransitionRejected(f"{recommendation_ref}: unknown recommendation_ref")
        return row

    def _cas_uncommitted(self, recommendation_ref: str, expected_state: str, next_state: str) -> bool:
        """The CAS update inside the caller's open transaction; the caller commits or rolls back."""
        cur = self._conn.execute(
            "UPDATE recommendation SET state = ? WHERE recommendation_ref = ? AND state = ?",
            (next_state, recommendation_ref, expected_state),
        )
        return cur.rowcount == 1

    def _cas(self, recommendation_ref: str, expected_state: str, next_state: str) -> bool:
        with self._conn:  # a failed write (e.g. database is locked) rolls back and leaves no open transaction
            return self._cas_uncommitted(recommendation_ref, expected_state, next_state)

    def _request_row(self, person_id: str, request_id: str) -> Optional[tuple]:
        return self._conn.execute(
            "SELECT request_json, outcome, recommendation_ref, method_code, reason_code "
            "FROM request WHERE person_id = ? AND request_id = ?",
            (person_id, request_id),
        ).fetchone()

    def _replay(self, person_id: str, request_id: str, request_json: str, row: tuple) -> RequestResult:
        stored_request, outcome, recommendation_ref, method_code, reason_code = row
        if stored_request != request_json:
            raise RequestConflict(
                f"{person_id}/{request_id}: request_id reused with different parameters, outcome or method"
            )
        return RequestResult(
            person_id, request_id, outcome, recommendation_ref, method_code, reason_code, replayed=True
        )

    def _open_request(
        self,
        person_id: str,
        request_id: str,
        params: dict,
        outcome: str,
        raw_ref: Optional[str] = None,
        method_code: Optional[str] = None,
        reason_code: Optional[str] = None,
    ) -> RequestResult:
        """Store one request (and its recommendation row, if any) atomically; replay if it already exists."""
        request_json = _canonical_json(
            {
                "params": params,
                "outcome": outcome,
                "raw_ref": raw_ref,
                "method_code": method_code,
                "reason_code": reason_code,
            }
        )
        with self._lock:
            row = self._request_row(person_id, request_id)
            if row is not None:
                return self._replay(person_id, request_id, request_json, row)
            recommendation_ref = f"rec_{uuid.uuid4()}" if outcome == OUTCOME_RECOMMENDATION else None
            try:
                with self._conn:
                    if recommendation_ref is not None:
                        self._conn.execute(
                            "INSERT INTO recommendation (recommendation_ref, raw_ref, state) VALUES (?, ?, ?)",
                            (recommendation_ref, raw_ref, CREATED),
                        )
                    self._conn.execute(
                        "INSERT INTO request (person_id, request_id, request_json, outcome, "
                        "recommendation_ref, method_code, reason_code, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (person_id, request_id, request_json, outcome, recommendation_ref,
                         method_code, reason_code, self._now()),
                    )
            except sqlite3.IntegrityError:
                # Another instance on the same database won the race for this key.
                row = self._request_row(person_id, request_id)
                if row is None:
                    raise
                return self._replay(person_id, request_id, request_json, row)
        return RequestResult(
            person_id, request_id, outcome, recommendation_ref, method_code, reason_code, replayed=False
        )

    def request_recommendation(
        self, person_id: str, request_id: str, params: dict, raw_ref: str, method_code: str
    ) -> RequestResult:
        """
        Idempotent by (person_id, request_id). The same key with the same parameters, raw_ref and
        method_code replays the stored outcome; any difference, or a refusal stored under that key,
        raises RequestConflict. method_code must be a non-empty ASCII code (it keys the 7-day window).
        """
        _require_ascii_code("method_code", method_code)
        return self._open_request(
            person_id, request_id, params, OUTCOME_RECOMMENDATION, raw_ref=raw_ref, method_code=method_code
        )

    def refuse_request(self, person_id: str, request_id: str, params: dict, reason_code: str) -> RequestResult:
        """
        Store a refusal (no recommendation is created). A retry with the same data replays it; a new
        calculation needs a new request_id. reason_code must be a non-empty ASCII code.
        """
        _require_ascii_code("reason_code", reason_code)
        return self._open_request(person_id, request_id, params, OUTCOME_REFUSED, reason_code=reason_code)

    def replay_request(self, person_id: str, request_id: str, params: dict) -> Optional[RequestResult]:
        """
        The stored outcome for this key, or None when the key is new. A key that exists with other
        parameters raises RequestConflict. Unlike request_recommendation it takes no method, so a
        caller can replay before it selects anything (a retry must not depend on today's selection).
        """
        with self._lock:
            row = self._request_row(person_id, request_id)
            if row is None:
                return None
            stored_request, outcome, recommendation_ref, method_code, reason_code = row
            if json.loads(stored_request)["params"] != json.loads(_canonical_json(params)):
                raise RequestConflict(f"{person_id}/{request_id}: request_id reused with different parameters")
            return RequestResult(
                person_id, request_id, outcome, recommendation_ref, method_code, reason_code, replayed=True
            )

    def state_of(self, recommendation_ref: str) -> str:
        with self._lock:
            return self._row(recommendation_ref)[1]

    def mark_grounding_checked(self, recommendation_ref: str) -> None:
        with self._lock:
            self._row(recommendation_ref)  # raises on unknown ref
            if not self._cas(recommendation_ref, CREATED, GROUNDING_CHECKED):
                raise TransitionRejected(f"{recommendation_ref}: expected state {CREATED}")

    def mark_shown(self, recommendation_ref: str) -> None:
        with self._lock:
            self._row(recommendation_ref)
            if not self._cas(recommendation_ref, GROUNDING_CHECKED, SHOWN):
                raise TransitionRejected(f"{recommendation_ref}: expected state {GROUNDING_CHECKED}")

    def confirm_issued(
        self, recommendation_ref: str, surface: str, session_ref: Optional[str] = None
    ) -> Optional[RecommendationIssued]:
        """
        Idempotent delivery confirmation: SHOWN -> ISSUED.

        Returns the event on the call that wins the CAS. Returns None (no new
        event) when the ref is already ISSUED -- a retried confirm after the
        transition already landed -- or when it lost the race to `expire()`.
        None is also returned, with no state change, for a ref that was never shown
        (created, grounding_checked, failed): the PR #6 contract, pinned by
        test_confirm_before_shown_is_noop. So None does NOT mean "delivery confirmed":
        a caller that needs certainty checks state_of(ref) == ISSUED.

        For a ref opened through request_recommendation() the state change, the
        issue history row and the outbox row are one transaction: if recording
        fails, the state stays SHOWN and nothing is half-written. A ref made by
        plain create() has no request row and gets the state change only.
        """
        with self._lock:
            raw_ref, state = self._row(recommendation_ref)
            if state == ISSUED:
                return None
            with self._conn:
                if not self._cas_uncommitted(recommendation_ref, SHOWN, ISSUED):
                    return None
                event = RecommendationIssued(
                    recommendation_ref=recommendation_ref,
                    raw_ref=raw_ref,
                    surface=surface,
                    issued_at=self._now(),
                    session_ref=session_ref,
                )
                self._record_issue(event)
        return event

    def _record_issue(self, event: RecommendationIssued) -> None:
        """Append the history row and the outbox row in the caller's open transaction."""
        row = self._conn.execute(
            "SELECT person_id, method_code FROM request WHERE recommendation_ref = ?",
            (event.recommendation_ref,),
        ).fetchone()
        if row is None:
            return
        person_id, method_code = row
        self._conn.execute(
            "INSERT INTO issue_history (recommendation_ref, person_id, method_code, issued_at) "
            "VALUES (?, ?, ?, ?)",
            (event.recommendation_ref, person_id, method_code, event.issued_at),
        )
        self._conn.execute(
            "INSERT INTO event_outbox (event_id, event_type, recommendation_ref, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                f"{event.recommendation_ref}:{EVENT_RECOMMENDATION_ISSUED}",
                EVENT_RECOMMENDATION_ISSUED,
                event.recommendation_ref,
                _canonical_json({**asdict(event), "person_id": person_id, "method_code": method_code}),
                event.issued_at,
            ),
        )

    def methods_issued_since(self, person_id: str, days: int = 7) -> set:
        """Method codes CONFIRMED as issued to this person within the last `days` days."""
        cutoff = datetime.strptime(self._now(), _ISO_FORMAT) - timedelta(days=days)
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT method_code FROM issue_history WHERE person_id = ? AND issued_at >= ?",
                (person_id, cutoff.strftime(_ISO_FORMAT)),
            ).fetchall()
        return {method_code for (method_code,) in rows}

    def pending_events(self) -> list:
        """Outbox events not yet marked delivered, oldest first: [{event_id, event_type, payload}]."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT event_id, event_type, payload_json FROM event_outbox "
                "WHERE delivered_at IS NULL ORDER BY created_at, event_id"
            ).fetchall()
        return [
            {"event_id": event_id, "event_type": event_type, "payload": json.loads(payload_json)}
            for event_id, event_type, payload_json in rows
        ]

    def mark_event_delivered(self, event_id: str) -> None:
        """Idempotent: a second call keeps the first delivery time. An unknown event_id raises ValueError."""
        with self._lock:
            known = self._conn.execute("SELECT 1 FROM event_outbox WHERE event_id = ?", (event_id,)).fetchone()
            if known is None:
                raise ValueError(f"unknown event_id: {event_id}")
            with self._conn:
                self._conn.execute(
                    "UPDATE event_outbox SET delivered_at = ? WHERE event_id = ? AND delivered_at IS NULL",
                    (self._now(), event_id),
                )

    def expire(self, recommendation_ref: str) -> Optional[RecommendationExpired]:
        """TTL sweep: SHOWN -> EXPIRED. Returns None if it lost the race to confirm_issued()."""
        with self._lock:
            raw_ref, _state = self._row(recommendation_ref)
            if not self._cas(recommendation_ref, SHOWN, EXPIRED):
                return None
        return RecommendationExpired(
            recommendation_ref=recommendation_ref, raw_ref=raw_ref, expired_at=self._now()
        )

    def mark_failed(self, recommendation_ref: str, stage: str, reason: str = "") -> RecommendationFailed:
        """stage="grounding": created->failed. stage="render": grounding_checked->failed."""
        if stage not in ("grounding", "render"):
            raise ValueError(f"unknown stage: {stage!r}")
        expected = CREATED if stage == "grounding" else GROUNDING_CHECKED
        with self._lock:
            raw_ref, _state = self._row(recommendation_ref)
            if not self._cas(recommendation_ref, expected, FAILED):
                raise TransitionRejected(f"{recommendation_ref}: expected state {expected} for stage={stage}")
        return RecommendationFailed(
            recommendation_ref=recommendation_ref,
            raw_ref=raw_ref,
            stage=stage,
            failed_at=self._now(),
            reason=reason,
        )
