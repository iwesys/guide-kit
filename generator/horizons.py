"""
agents/tailor/horizons.py — Types for Tailor-2 (horizon cascade + RCS input).
# see WP-149 Phase 11, WP-203 Phase 1.5

Contains only data types — selection business logic lives in planner.py.

Two input formats for RCS:
  - full (WP-151 Phase 12): {worldview: N, mastery: {m1_focus: N, ...}, it_level: N, agency: N}
  - compact (render-pilot-guides.py): {W: N, M1: N, M2: N, M4: N, stage: N}
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional


# ─────────────────────────────────────────────────────────────────────────────
# RCS Profile (PD.FORM.089, 7 slots)
# ─────────────────────────────────────────────────────────────────────────────

SLOT_LABELS: dict[str, str] = {
    "W":  "Мировоззрение",
    "M1": "Фокус и собранность",
    "M2": "IWE / ОРЗ",
    "M3": "Домен",
    "M4": "Системное мышление",
    "IT": "ИТ-уровень",
    "A":  "Агентность",
}

_ALL_SLOTS = ("W", "M1", "M2", "M3", "M4", "IT", "A")

# Compact key names recognized by RCSProfile
_COMPACT_KEYS = frozenset({
    "W", "M1", "M2", "M3", "M4", "IT", "A",
    "bottleneck", "stage_derived", "source", "confidence",
})

# Full-format (WP-151) sub-keys under "mastery" → compact names
_MASTERY_SUB = {
    "m1_focus": "M1",
    "m2_iwe": "M2",
    "m3_domain": "M3",
    "m4_systems": "M4",
}


def normalize_rcs_dict(d: dict) -> dict:
    """Normalize any RCS dict (full or compact format) to compact keys.

    Compact key set: W, M1-M4, IT, A, bottleneck, stage_derived, source, confidence.
    Only keys actually present in *d* appear in the output — no defaults are injected.
    from_dict() reuses this function so the key-mapping table is never duplicated.
    """
    if "worldview" not in d:
        # Already compact (or aliases only) — rename known aliases, drop unknowns
        result: dict = {}
        for k, v in d.items():
            if k == "stage":
                result["stage_derived"] = v
            elif k == "it_level":
                result["IT"] = v
            elif k == "agency":
                result["A"] = v
            elif k in _COMPACT_KEYS:
                result[k] = v
        return result

    # Full format (WP-151 Phase 12)
    mastery = d.get("mastery") or {}
    result = {"W": d["worldview"]}
    for sub, compact in _MASTERY_SUB.items():
        if sub in mastery:
            result[compact] = mastery[sub]
    if "it_level" in d:
        result["IT"] = d["it_level"]
    if "agency" in d:
        result["A"] = d["agency"]
    for k in ("bottleneck", "stage_derived", "source", "confidence"):
        if k in d:
            result[k] = d[k]
    return result


@dataclass
class RCSProfile:
    """RCS profile (PD.FORM.089, 7 slots), each 1-5."""

    W:  int = 1   # Worldview
    M1: int = 1   # Focus and self-organization
    M2: int = 1   # IWE / ORZ (Opening-Work-Closing)
    M3: int = 1   # Domain
    M4: int = 1   # Systems thinking
    IT: int = 1   # IT proficiency
    A:  int = 1   # Agency

    bottleneck: str = "M1"     # slot with the largest gap
    stage_derived: int = 1     # computed stage (1-5)
    source: str = "manual"     # diagnostic_session | computed_from_events | manual
    confidence: float = 0.0    # confidence of the computation (0..1)

    @classmethod
    def from_dict(cls, d: dict) -> "RCSProfile":
        """Build from a dict. Supports both the full and the compact format."""
        c = normalize_rcs_dict(d)
        return cls(
            W=int(c.get("W", 1)),
            M1=int(c.get("M1", 1)),
            M2=int(c.get("M2", 1)),
            M3=int(c.get("M3", 1)),
            M4=int(c.get("M4", 1)),
            IT=int(c.get("IT", 1)),
            A=int(c.get("A", 1)),
            bottleneck=str(c.get("bottleneck", "M1")),
            stage_derived=int(c.get("stage_derived", 1)),
            source=str(c.get("source", "manual")),
            confidence=float(c.get("confidence", 0.0)),
        )

    def weakest_slots(self, n: int = 2) -> list[str]:
        """n slots with the lowest value (bottleneck candidates)."""
        vals = [(s, getattr(self, s)) for s in _ALL_SLOTS]
        return [s for s, _ in sorted(vals, key=lambda x: x[1])[:n]]

    def to_dict(self) -> dict:
        return {
            "W": self.W, "M1": self.M1, "M2": self.M2, "M3": self.M3,
            "M4": self.M4, "IT": self.IT, "A": self.A,
            "bottleneck": self.bottleneck,
            "stage_derived": self.stage_derived,
            "source": self.source,
            "confidence": self.confidence,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator triggers (WP-203 Phase 1.5)
# ─────────────────────────────────────────────────────────────────────────────

TriggerKind = Literal[
    "routine",          # scheduled run, no events
    "slot_miss",        # slot missed (no completed session for N days)
    "focus_shift",      # user reported a topic change
    "metric_jump",      # sharp rise/drop in an RCS slot
    "calendar_event",   # conference, business trip, release
    "blocker",          # artifacts haven't grown along this branch for >=2 weeks
    "hypothesis_fail",  # week-end check: expected != actual
]

# Tactical (adjust the assignment), strategic (change the hypothesis/focus)
TACTICAL_TRIGGERS: frozenset[str] = frozenset({"routine", "slot_miss", "calendar_event"})
STRATEGIC_TRIGGERS: frozenset[str] = frozenset({"focus_shift", "metric_jump", "blocker", "hypothesis_fail"})


@dataclass
class OrchestratorTrigger:
    kind: TriggerKind = "routine"
    detail: str = ""     # human-readable description of the event
    severity: int = 0    # 0=routine, 1=tactical, 2=strategic, 3=escalate

    def is_tactical(self) -> bool:
        return self.kind in TACTICAL_TRIGGERS

    def is_strategic(self) -> bool:
        return self.kind in STRATEGIC_TRIGGERS


# ─────────────────────────────────────────────────────────────────────────────
# Four horizons
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class QuarterFocus:
    """Quarterly horizon — the quarter's focus and bottleneck."""
    bottleneck_slot: str = ""              # RCS slot that is the quarter's priority
    theme: str = ""                        # quarter's theme, as text
    target_delta: dict[str, int] = field(default_factory=dict)  # {slot: target_increase}


@dataclass
class MonthThemes:
    """Monthly horizon — 1-3 memes/methods for the month."""
    memes: list[str] = field(default_factory=list)    # element_id from CAT.001
    methods: list[str] = field(default_factory=list)  # element_id from CAT.003
    label: str = ""     # theme as text (contents of monthly-theme.md)


@dataclass
class WeekHypothesis:
    """Weekly horizon — this week's hypothesis."""
    expected_delta: dict[str, float] = field(default_factory=dict)  # {slot: expected_increase}
    slack_budget: float = 0.2   # allowed share of misses (0..1)
    focus_area: int = 0         # FORM.081 area (0=auto, from the RCS bottleneck)
    label: str = ""             # hypothesis description, as text


@dataclass
class DayEvents:
    """Tactical input from the Orchestrator — the day's events."""
    missed_slots: int = 0           # sessions missed over the last N days
    calendar_load: Literal["light", "normal", "heavy"] = "normal"
    energy_override: Optional[int] = None   # set if the Orchestrator knows better (1-5)
    notes: str = ""                 # free-form notes


@dataclass
class ArtifactsSummary:
    """What the user created over the period (from the WP-109 classifier)."""
    count: int = 0
    by_type: dict[str, int] = field(default_factory=dict)    # {artifact_type: count}
    recent_titles: list[str] = field(default_factory=list)   # last N titles


# ─────────────────────────────────────────────────────────────────────────────
# HorizonContext — Tailor-2's main input
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class HorizonContext:
    """Tailor-2's full input (WP-149 Phase 11): RCS + 4 horizons + artifacts + trigger.

    Replaces the flat TailorContext for horizon-aware mode.
    Compatible with the current render-pilot-guides.py via from_render_context().
    """

    rcs: RCSProfile
    trigger: OrchestratorTrigger = field(default_factory=OrchestratorTrigger)

    # 4 horizons (empty fields -> planner.py derives them from RCS)
    quarter: QuarterFocus = field(default_factory=QuarterFocus)
    month: MonthThemes = field(default_factory=MonthThemes)
    week: WeekHypothesis = field(default_factory=WeekHypothesis)
    day: DayEvents = field(default_factory=DayEvents)

    artifacts: ArtifactsSummary = field(default_factory=ArtifactsSummary)
    summary_events: str = ""    # events from render-pilot-guides.py (B2)
    mastery_by_area: dict = field(default_factory=dict)  # Phase 4.1: {area_key: depth} from Memory.Derived
    pilot_reflection: str = ""  # pilot's reflection from yesterday (history/YYYY-MM-DD-reflection.txt)
    reflection_learned: list[str] = field(default_factory=list)  # Q3 "What I learned" over 7 days
    tomorrow_intention: str = ""  # Q5 "What's next" from the latest reflection

    def effective_bottleneck(self) -> str:
        """Effective bottleneck: quarterly if set, otherwise from RCS."""
        return self.quarter.bottleneck_slot or self.rcs.bottleneck

    def effective_focus_area(self) -> int:
        """Week's area: from the weekly hypothesis, or 0 (planner will choose)."""
        return self.week.focus_area

    def energy(self) -> int:
        """Energy: Orchestrator override, or default 3.

        Supports energy_override=0 (very low energy) — does not replace it with 3.
        """
        return 3 if self.day.energy_override is None else self.day.energy_override

    @classmethod
    def from_render_context(
        cls,
        rcs_dict: dict,
        events_summary: str = "",
        monthly_theme_md: str = "",
    ) -> "HorizonContext":
        """Compatibility constructor for render-pilot-guides.py.

        Used until the Orchestrator (WP-203) is fully implemented.
        The quarter/week/day horizons stay empty — planner.py will fill them from RCS.
        """
        rcs = RCSProfile.from_dict(rcs_dict)
        month = MonthThemes(label=(monthly_theme_md or "")[:500])
        return cls(
            rcs=rcs,
            month=month,
            summary_events=events_summary,
        )


# ─────────────────────────────────────────────────────────────────────────────
# PlanDay — Tailor-2's output
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class DZItem:
    """One assignment in the daily package."""
    element_id: str
    element_type: str    # worldview | mastery
    area: int            # 1-5 (FORM.081)
    target_depth: int    # depth of the study session
    tomatoes: int = 1    # expected number of pomodoro sessions
    label: str = ""      # short title
    rationale: str = ""  # why this particular item today


@dataclass
class PlanDay:
    """Tailor-2's output — daily assignment package + narrative (WP-149 Phase 11).

    Replaces plan()'s dict output for horizon-aware mode.
    Compatible: plan() returns a dict, plan_horizon() returns a PlanDay.
    """
    items: list[DZItem]
    narrative: str              # 1-2 paragraphs on "why this particular item today"
    week_label: str = ""        # ISO week (2026-W19)
    trigger_response: str = ""  # response to the Orchestrator's trigger
    decision_log: dict = field(default_factory=dict)

    def total_tomatoes(self) -> int:
        return sum(i.tomatoes for i in self.items)

    def to_dict(self) -> dict:
        return {
            "items": [
                {
                    "element_id": it.element_id,
                    "element_type": it.element_type,
                    "area": it.area,
                    "target_depth": it.target_depth,
                    "tomatoes": it.tomatoes,
                    "label": it.label,
                    "rationale": it.rationale,
                }
                for it in self.items
            ],
            "narrative": self.narrative,
            "week_label": self.week_label,
            "trigger_response": self.trigger_response,
            "total_tomatoes": self.total_tomatoes(),
            "decision_log": self.decision_log,
        }
