"""
Server recommendation flow (WP-521 F12): choose a method from the catalog snapshot, issue it
through the registry, confirm delivery. A thin layer over RecommendationRegistry; it owns the
selection and the three inputs the registry knows nothing about.

Inputs and what is real today (peer session 2026-11-25):
- Catalog snapshot: a JSON file built outside this repository (it carries method texts in the
  author's language, which this repository does not allow). Only methods with necessity "core",
  with material for the next action and the criterion, with every prerequisite already done by
  the person, and not done themselves are eligible. Umbrella methods have no material of their
  own, so they never are.
- Order among eligible methods: the catalog's own order field, then the code. That order is a
  working hypothesis of the catalog owners, not a rule; every recommendation carries the flag
  `selection_provisional` so a consumer never presents it as one.
- Slot of the person's bottleneck: read by the caller from the diagnostics table and passed in.
  There is no data that maps a method to a slot, so the slot is returned as information only and
  never filters. No slot gives the flag `slot_unknown`.
- Methods the person already passed (checklist): the mapping between checklist facts and catalog
  codes is not connected, so the caller passes an empty set; then a method with prerequisites is
  not eligible (a prerequisite is never assumed done).
- Grounding gate (WP-498): not integrated. The `show` command records the check as passed on the
  caller's word; it exists for the test stage and must be replaced by the real gate.

Reason codes of a refusal: `no_verifiable_method`, `all_recently_issued`.
Replay: a retry with the same (person_id, request_id) and the same parameters returns the stored
outcome without selecting again, so it never depends on today's history, and two identical
requests racing each other both get the one stored outcome. The set of methods the person
already passed is not part of the key: a retry with another set still returns the stored outcome.
If the catalog snapshot changed since the first request so that the method's material no longer
matches the stored hash, the replay says so (`material_changed`; the catalog revision alone does not count) and returns no texts: a person
is never shown a text that differs from the one registered; a new calculation needs a new
request_id. A recommendation that expired or failed is returned with its state and the flag
`recommendation_not_live`.

Command line: `init` creates the database; every other command refuses a missing file, so a
typo in --db never starts an empty history. Failures print one JSON object with an `error`
code and exit with status 2: request_conflict, transition_rejected, invalid_input,
database_not_found, database_error.
"""

import argparse
import json
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from typing import Optional

from recommendation_registry import (
    CREATED,
    EXPIRED,
    FAILED,
    GROUNDING_CHECKED,
    ISSUED,
    OUTCOME_RECOMMENDATION,
    SHOWN,
    RecommendationIssued,
    RecommendationRegistry,
    RequestConflict,
    RequestResult,
    TransitionRejected,
    compute_content_hash,
)

CORE = "core"
REASON_NO_METHOD = "no_verifiable_method"
REASON_ALL_RECENT = "all_recently_issued"
FLAG_SLOT_UNKNOWN = "slot_unknown"
FLAG_SELECTION_PROVISIONAL = "selection_provisional"
FLAG_MATERIAL_UNAVAILABLE = "material_unavailable"
FLAG_MATERIAL_CHANGED = "material_changed"
FLAG_NOT_LIVE = "recommendation_not_live"
WINDOW_DAYS = 7
RESERVED_PARAM_KEYS = ("slot", "slot_cut_date")


@dataclass(frozen=True)
class Method:
    code: str
    necessity: str
    order: int
    prerequisites: tuple
    next_action: Optional[str]
    criterion: Optional[str]

    @property
    def has_material(self) -> bool:
        return bool(self.next_action) and bool(self.criterion)


@dataclass(frozen=True)
class CatalogSnapshot:
    source_revision: str
    methods: dict


@dataclass
class FlowOutcome:
    result: RequestResult
    method: Optional[Method]
    slot: Optional[str]
    slot_cut_date: Optional[str]
    flags: tuple = field(default_factory=tuple)
    state: Optional[str] = None  # registry state of the recommendation; None for a refusal


@dataclass
class DeliveryConfirmation:
    recommendation_ref: str
    event: Optional[RecommendationIssued]  # None: delivery was already confirmed by an earlier call


def load_catalog_snapshot(path: str) -> CatalogSnapshot:
    with open(path, encoding="utf-8") as handle:
        raw = json.load(handle)
    methods = {}
    for entry in raw["methods"]:
        material = entry.get("material") or {}
        if not entry["code"] or not entry["code"].isascii():
            raise ValueError(f"method code must be a non-empty ASCII string, got {entry['code']!r}")
        methods[entry["code"]] = Method(
            code=entry["code"],
            necessity=entry["necessity"],
            order=int(entry["order"]),
            prerequisites=tuple(entry.get("prerequisites", [])),
            next_action=material.get("next_action"),
            criterion=material.get("criterion"),
        )
    return CatalogSnapshot(source_revision=raw["source_revision"], methods=methods)


def eligible_methods(snapshot: CatalogSnapshot, done_codes: frozenset) -> list:
    """Core methods with material, not yet done, whose prerequisites are all done; catalog order."""
    candidates = [
        m
        for m in snapshot.methods.values()
        if m.necessity == CORE
        and m.has_material
        and m.code not in done_codes
        and all(prerequisite in done_codes for prerequisite in m.prerequisites)
    ]
    return sorted(candidates, key=lambda m: (m.order, m.code))


def _raw_ref(method: Method) -> str:
    """Hash of what the person is shown. The catalog revision is deliberately not part of it: the
    catalog is edited often, and a new revision with the same texts must not turn every retry of an
    earlier request into `material_changed`."""
    payload = json.dumps(
        {
            "method": method.code,
            "next_action": method.next_action,
            "criterion": method.criterion,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return compute_content_hash(payload)


class RecommendationFlow:
    def __init__(self, registry: RecommendationRegistry, snapshot: CatalogSnapshot):
        self._registry = registry
        self._snapshot = snapshot

    def request(
        self,
        person_id: str,
        request_id: str,
        params: dict,
        slot: Optional[str] = None,
        slot_cut_date: Optional[str] = None,
        done_codes: frozenset = frozenset(),
    ) -> FlowOutcome:
        """
        Idempotent by (person_id, request_id). The slot and its date are part of the stored
        parameters: the same key with another slot is a RequestConflict, like any other change.
        """
        reserved = [key for key in RESERVED_PARAM_KEYS if key in params]
        if reserved:
            raise ValueError(f"params must not contain {reserved}: pass the slot as its own argument")
        key_params = {**params, "slot": slot, "slot_cut_date": slot_cut_date}
        stored = self._registry.replay_request(person_id, request_id, key_params)
        if stored is None:
            try:
                stored = self._issue_new(person_id, request_id, key_params, done_codes)
            except RequestConflict:
                # An identical request raced this one and stored a different pick. The stored
                # outcome wins when the parameters match; replay_request raises if they do not.
                stored = self._registry.replay_request(person_id, request_id, key_params)
                if stored is None:
                    raise
        return self._outcome(stored, slot, slot_cut_date)

    def _issue_new(self, person_id: str, request_id: str, key_params: dict, done_codes: frozenset) -> RequestResult:
        candidates = eligible_methods(self._snapshot, done_codes)
        recent = self._registry.methods_issued_since(person_id, days=WINDOW_DAYS)
        fresh = [m for m in candidates if m.code not in recent]
        if not fresh:
            reason = REASON_ALL_RECENT if candidates else REASON_NO_METHOD
            return self._registry.refuse_request(person_id, request_id, key_params, reason)
        chosen = fresh[0]
        return self._registry.request_recommendation(
            person_id, request_id, key_params, _raw_ref(chosen), chosen.code
        )

    def _outcome(self, result: RequestResult, slot: Optional[str], slot_cut_date: Optional[str]) -> FlowOutcome:
        flags = []
        method = None
        state = None
        if result.outcome == OUTCOME_RECOMMENDATION:
            state = self._registry.state_of(result.recommendation_ref)
            flags.append(FLAG_SELECTION_PROVISIONAL)
            method = self._snapshot.methods.get(result.method_code)
            if method is None or not method.has_material:
                flags.append(FLAG_MATERIAL_UNAVAILABLE)
                method = None
            elif _raw_ref(method) != result.raw_ref:
                flags.append(FLAG_MATERIAL_CHANGED)
                method = None
            if state in (EXPIRED, FAILED):
                flags.append(FLAG_NOT_LIVE)
        if slot is None:
            flags.append(FLAG_SLOT_UNKNOWN)
        return FlowOutcome(result, method, slot, slot_cut_date, tuple(flags), state)

    def show(self, recommendation_ref: str) -> str:
        """
        Test stage: record the grounding check as passed on the caller's word and the show as done.
        Safe to repeat and to resume after a crash between the two steps; returns the state reached.
        """
        state = self._registry.state_of(recommendation_ref)
        if state == CREATED:
            self._registry.mark_grounding_checked(recommendation_ref)
            state = GROUNDING_CHECKED
        if state == GROUNDING_CHECKED:
            self._registry.mark_shown(recommendation_ref)
            state = SHOWN
        if state not in (SHOWN, ISSUED):
            raise TransitionRejected(f"{recommendation_ref}: cannot show from state {state}")
        return state

    def confirm_delivery(
        self, recommendation_ref: str, surface: str, session_ref: Optional[str] = None
    ) -> DeliveryConfirmation:
        """
        Confirm that the person was shown the recommendation. Raises TransitionRejected unless the
        recommendation ends up issued, so a caller never reads "nothing returned" as success.
        """
        event = self._registry.confirm_issued(recommendation_ref, surface=surface, session_ref=session_ref)
        state = self._registry.state_of(recommendation_ref)
        if state != ISSUED:
            raise TransitionRejected(f"{recommendation_ref}: delivery not confirmed, state is {state}")
        return DeliveryConfirmation(recommendation_ref, event)


def _outcome_json(outcome: FlowOutcome) -> dict:
    result = outcome.result
    method = outcome.method
    return {
        "outcome": result.outcome,
        "replayed": result.replayed,
        "recommendation_ref": result.recommendation_ref,
        "state": outcome.state,
        "method_code": result.method_code,
        "reason_code": result.reason_code,
        "next_action": method.next_action if method else None,
        "criterion": method.criterion if method else None,
        "slot": outcome.slot,
        "slot_cut_date": outcome.slot_cut_date,
        "flags": list(outcome.flags),
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="recommendation_flow")
    parser.add_argument("--db", required=True, help="registry database file (no default on purpose)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="create the database file; every other command refuses a missing file")
    request = commands.add_parser("request", help="issue or replay a recommendation")
    request.add_argument("--snapshot", required=True)
    request.add_argument("--person", required=True)
    request.add_argument("--request-id", required=True)
    request.add_argument("--params-json", required=True, help='JSON object, for example {"case": "...", "probe": "..."}')
    request.add_argument("--slot")
    request.add_argument("--slot-cut-date")
    show = commands.add_parser("show", help="test stage: record grounding as passed and the show as done")
    show.add_argument("--ref", required=True)
    confirm = commands.add_parser("confirm", help="confirm the delivery of a shown recommendation")
    confirm.add_argument("--ref", required=True)
    confirm.add_argument("--surface", required=True)
    confirm.add_argument("--session-ref")
    commands.add_parser("events", help="list outbox events not delivered yet")
    delivered = commands.add_parser("delivered", help="mark an outbox event as delivered")
    delivered.add_argument("--event-id", required=True)
    return parser


def _run_command(args: argparse.Namespace, registry: RecommendationRegistry) -> dict:
    if args.command == "init":
        return {"database": args.db, "created": True}
    if args.command == "request":
        flow = RecommendationFlow(registry, load_catalog_snapshot(args.snapshot))
        params = json.loads(args.params_json)
        if not isinstance(params, dict):
            raise ValueError("--params-json must be a JSON object")
        return _outcome_json(flow.request(args.person, args.request_id, params, args.slot, args.slot_cut_date))
    flow = RecommendationFlow(registry, CatalogSnapshot("none", {}))
    if args.command == "show":
        return {"recommendation_ref": args.ref, "state": flow.show(args.ref)}
    if args.command == "confirm":
        confirmation = flow.confirm_delivery(args.ref, args.surface, args.session_ref)
        return {"recommendation_ref": args.ref, "state": ISSUED, "new": confirmation.event is not None}
    if args.command == "events":
        return {"events": registry.pending_events()}
    registry.mark_event_delivered(args.event_id)
    return {"event_id": args.event_id, "delivered": True}


# Exceptions mapped to a stable error code; anything else is a bug and keeps its traceback.
_ERROR_CODES = (
    (RequestConflict, "request_conflict"),
    (TransitionRejected, "transition_rejected"),
    (ValueError, "invalid_input"),  # includes json.JSONDecodeError
    (KeyError, "invalid_input"),
    (FileNotFoundError, "invalid_input"),
    (sqlite3.DatabaseError, "database_error"),
)


def main(argv: Optional[list] = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command != "init" and not os.path.exists(args.db):
        print(json.dumps({"error": "database_not_found", "message": f"{args.db}: run init first"}))
        return 2
    registry = None
    try:
        registry = RecommendationRegistry(args.db)
        print(json.dumps(_run_command(args, registry), ensure_ascii=False))
        return 0
    except tuple(kind for kind, _ in _ERROR_CODES) as exc:
        code = next(name for kind, name in _ERROR_CODES if isinstance(exc, kind))
        print(json.dumps({"error": code, "message": str(exc)}, ensure_ascii=False))
        return 2
    finally:
        if registry is not None:
            registry.close()


if __name__ == "__main__":
    sys.exit(main())
