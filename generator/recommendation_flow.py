"""
Server recommendation flow (WP-521 F12): choose a method from the catalog snapshot, issue it
through the registry, confirm delivery. A thin layer over RecommendationRegistry; it owns the
selection and the three inputs the registry knows nothing about.

Inputs and what is real today (peer session 2026-11-25):
- Catalog snapshot: a JSON file built outside this repository (it carries method texts in the
  author's language, which this repository does not allow). Only methods with level "core",
  with material for the next action and the criterion, and with every prerequisite already done
  by the person are eligible. Umbrella methods have no material of their own, so they never are.
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
outcome without selecting again, so it never depends on today's history.
"""

import argparse
import json
import sys
from dataclasses import dataclass, field
from typing import Optional

from recommendation_registry import (
    ISSUED,
    OUTCOME_RECOMMENDATION,
    RecommendationIssued,
    RecommendationRegistry,
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
WINDOW_DAYS = 7


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


def _raw_ref(method: Method, snapshot: CatalogSnapshot) -> str:
    payload = json.dumps(
        {
            "method": method.code,
            "next_action": method.next_action,
            "criterion": method.criterion,
            "catalog_revision": snapshot.source_revision,
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
        key_params = {**params, "slot": slot, "slot_cut_date": slot_cut_date}
        stored = self._registry.replay_request(person_id, request_id, key_params)
        if stored is not None:
            return self._outcome(stored, slot, slot_cut_date)
        result = self._issue_new(person_id, request_id, key_params, done_codes)
        return self._outcome(result, slot, slot_cut_date)

    def _issue_new(self, person_id: str, request_id: str, key_params: dict, done_codes: frozenset) -> RequestResult:
        candidates = eligible_methods(self._snapshot, done_codes)
        recent = self._registry.methods_issued_since(person_id, days=WINDOW_DAYS)
        fresh = [m for m in candidates if m.code not in recent]
        if not fresh:
            reason = REASON_ALL_RECENT if candidates else REASON_NO_METHOD
            return self._registry.refuse_request(person_id, request_id, key_params, reason)
        chosen = fresh[0]
        return self._registry.request_recommendation(
            person_id, request_id, key_params, _raw_ref(chosen, self._snapshot), chosen.code
        )

    def _outcome(self, result: RequestResult, slot: Optional[str], slot_cut_date: Optional[str]) -> FlowOutcome:
        flags = []
        method = None
        if result.outcome == OUTCOME_RECOMMENDATION:
            method = self._snapshot.methods.get(result.method_code)
            flags.append(FLAG_SELECTION_PROVISIONAL)
            if method is None or not method.has_material:
                flags.append(FLAG_MATERIAL_UNAVAILABLE)
        if slot is None:
            flags.append(FLAG_SLOT_UNKNOWN)
        return FlowOutcome(result, method, slot, slot_cut_date, tuple(flags))

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


def main(argv: Optional[list] = None) -> int:
    args = _build_parser().parse_args(argv)
    registry = RecommendationRegistry(args.db)
    try:
        if args.command == "request":
            flow = RecommendationFlow(registry, load_catalog_snapshot(args.snapshot))
            outcome = flow.request(
                args.person, args.request_id, json.loads(args.params_json), args.slot, args.slot_cut_date
            )
            print(json.dumps(_outcome_json(outcome), ensure_ascii=False))
        elif args.command == "show":
            registry.mark_grounding_checked(args.ref)
            registry.mark_shown(args.ref)
            print(json.dumps({"recommendation_ref": args.ref, "state": registry.state_of(args.ref)}))
        elif args.command == "confirm":
            flow = RecommendationFlow(registry, CatalogSnapshot("none", {}))
            confirmation = flow.confirm_delivery(args.ref, args.surface, args.session_ref)
            print(json.dumps({"recommendation_ref": args.ref, "state": ISSUED, "new": confirmation.event is not None}))
        elif args.command == "events":
            print(json.dumps(registry.pending_events(), ensure_ascii=False))
        else:
            registry.mark_event_delivered(args.event_id)
            print(json.dumps({"event_id": args.event_id, "delivered": True}))
    finally:
        registry.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
