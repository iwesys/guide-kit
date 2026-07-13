"""
agents/tailor/horizons.py — Типы для Портного-2 (каскад горизонтов + RCS-вход).
# see WP-149 Ф11, WP-203 Ф1.5

Содержит только типы данных — бизнес-логика выбора в planner.py.

Два входных формата RCS:
  - полный (WP-151 Ф12): {worldview: N, mastery: {m1_focus: N, ...}, it_level: N, agency: N}
  - компактный (render-pilot-guides.py): {W: N, M1: N, M2: N, M4: N, stage: N}
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional


# ─────────────────────────────────────────────────────────────────────────────
# RCS Profile (PD.FORM.089, 7 слотов)
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


@dataclass
class RCSProfile:
    """RCS-профиль (PD.FORM.089, 7 слотов), каждый 1–5."""

    W:  int = 1   # Мировоззрение
    M1: int = 1   # Фокус и собранность
    M2: int = 1   # IWE / ОРЗ
    M3: int = 1   # Домен
    M4: int = 1   # Системное мышление
    IT: int = 1   # ИТ-уровень
    A:  int = 1   # Агентность

    bottleneck: str = "M1"     # слот с наибольшим разрывом
    stage_derived: int = 1     # вычисленная ступень (1–5)
    source: str = "manual"     # diagnostic_session | computed_from_events | manual
    confidence: float = 0.0    # уверенность расчёта (0..1)

    @classmethod
    def from_dict(cls, d: dict) -> "RCSProfile":
        """Создать из словаря. Поддерживает полный и компактный форматы."""
        if "worldview" in d:
            # Полный формат (WP-151 Ф12)
            mastery = d.get("mastery", {})
            return cls(
                W=int(d.get("worldview", 1)),
                M1=int(mastery.get("m1_focus", 1)),
                M2=int(mastery.get("m2_iwe", 1)),
                M3=int(mastery.get("m3_domain", 1)),
                M4=int(mastery.get("m4_systems", 1)),
                IT=int(d.get("it_level", 1)),
                A=int(d.get("agency", 1)),
                bottleneck=str(d.get("bottleneck", "M1")),
                stage_derived=int(d.get("stage_derived", 1)),
                source=str(d.get("source", "manual")),
                confidence=float(d.get("confidence", 0.0)),
            )
        # Компактный формат (render-pilot-guides.py)
        stage = int(d.get("stage", d.get("stage_derived", 1)))
        return cls(
            W=int(d.get("W", 1)),
            M1=int(d.get("M1", 1)),
            M2=int(d.get("M2", 1)),
            M3=int(d.get("M3", 1)),
            M4=int(d.get("M4", 1)),
            IT=int(d.get("IT", d.get("it_level", 1))),
            A=int(d.get("A", d.get("agency", 1))),
            bottleneck=str(d.get("bottleneck", "M1")),
            stage_derived=stage,
            source=str(d.get("source", "manual")),
            confidence=float(d.get("confidence", 0.0)),
        )

    def weakest_slots(self, n: int = 2) -> list[str]:
        """n слотов с наименьшим значением (кандидаты на bottleneck)."""
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
# Orchestrator triggers (WP-203 Ф1.5)
# ─────────────────────────────────────────────────────────────────────────────

TriggerKind = Literal[
    "routine",          # плановый запуск без событий
    "slot_miss",        # срыв слота (нет помидорки N дней)
    "focus_shift",      # пользователь сообщил смену темы
    "metric_jump",      # резкий рост/падение RCS-слота
    "calendar_event",   # конференция, командировка, релиз
    "blocker",          # артефакты не растут по ветке ≥2 нед
    "hypothesis_fail",  # week-end сверка: ожидаемо ≠ факт
]

# Тактические (корректируют ДЗ), стратегические (меняют гипотезу/фокус)
TACTICAL_TRIGGERS: frozenset[str] = frozenset({"routine", "slot_miss", "calendar_event"})
STRATEGIC_TRIGGERS: frozenset[str] = frozenset({"focus_shift", "metric_jump", "blocker", "hypothesis_fail"})


@dataclass
class OrchestratorTrigger:
    kind: TriggerKind = "routine"
    detail: str = ""     # человекочитаемое описание события
    severity: int = 0    # 0=routine, 1=tactical, 2=strategic, 3=escalate

    def is_tactical(self) -> bool:
        return self.kind in TACTICAL_TRIGGERS

    def is_strategic(self) -> bool:
        return self.kind in STRATEGIC_TRIGGERS


# ─────────────────────────────────────────────────────────────────────────────
# Четыре горизонта
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class QuarterFocus:
    """Квартальный горизонт — фокус и bottleneck квартала."""
    bottleneck_slot: str = ""              # слот RCS приоритета квартала
    theme: str = ""                        # текстовая тема квартала
    target_delta: dict[str, int] = field(default_factory=dict)  # {slot: целевой_прирост}


@dataclass
class MonthThemes:
    """Месячный горизонт — 1–3 мема/метода месяца."""
    memes: list[str] = field(default_factory=list)    # element_id из CAT.001
    methods: list[str] = field(default_factory=list)  # element_id из CAT.003
    label: str = ""     # текстовая тема (содержимое monthly-theme.md)


@dataclass
class WeekHypothesis:
    """Недельный горизонт — гипотеза недели."""
    expected_delta: dict[str, float] = field(default_factory=dict)  # {slot: ожидаемый_прирост}
    slack_budget: float = 0.2   # допустимая доля пропусков (0..1)
    focus_area: int = 0         # область FORM.081 (0=авто по RCS bottleneck)
    label: str = ""             # текстовое описание гипотезы


@dataclass
class DayEvents:
    """Тактический вход от Оркестратора — события дня."""
    missed_slots: int = 0           # помидорок пропущено за последние N дней
    calendar_load: Literal["light", "normal", "heavy"] = "normal"
    energy_override: Optional[int] = None   # если Оркестратор знает лучше (1–5)
    notes: str = ""                 # произвольные заметки


@dataclass
class ArtifactsSummary:
    """Что пользователь создал за период (из WP-109 classifier)."""
    count: int = 0
    by_type: dict[str, int] = field(default_factory=dict)    # {artifact_type: count}
    recent_titles: list[str] = field(default_factory=list)   # последние N названий


# ─────────────────────────────────────────────────────────────────────────────
# HorizonContext — главный вход Портного-2
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class HorizonContext:
    """Полный вход Портного-2 (WP-149 Ф11): RCS + 4 горизонта + артефакты + триггер.

    Заменяет плоский TailorContext для horizon-aware режима.
    Совместимость с текущим render-pilot-guides.py через from_render_context().
    """

    rcs: RCSProfile
    trigger: OrchestratorTrigger = field(default_factory=OrchestratorTrigger)

    # 4 горизонта (пустые поля → planner.py строит из RCS)
    quarter: QuarterFocus = field(default_factory=QuarterFocus)
    month: MonthThemes = field(default_factory=MonthThemes)
    week: WeekHypothesis = field(default_factory=WeekHypothesis)
    day: DayEvents = field(default_factory=DayEvents)

    artifacts: ArtifactsSummary = field(default_factory=ArtifactsSummary)
    summary_events: str = ""    # события из render-pilot-guides.py (B2)
    mastery_by_area: dict = field(default_factory=dict)  # Ф4.1: {area_key: depth} из Память.Derived
    pilot_reflection: str = ""  # рефлексия пилота вчера (history/YYYY-MM-DD-reflection.txt)
    reflection_learned: list[str] = field(default_factory=list)  # Q3 «Что узнал» за 7 дней
    tomorrow_intention: str = ""  # Q5 «Что завтра» из последней рефлексии

    def effective_bottleneck(self) -> str:
        """Актуальный bottleneck: квартальный если задан, иначе из RCS."""
        return self.quarter.bottleneck_slot or self.rcs.bottleneck

    def effective_focus_area(self) -> int:
        """Область недели: из недельной гипотезы или 0 (planner выберет сам)."""
        return self.week.focus_area

    def energy(self) -> int:
        """Энергия: override от Оркестратора или дефолт 3.

        Поддерживает energy_override=0 (очень низкая энергия) — не заменяет на 3.
        """
        return 3 if self.day.energy_override is None else self.day.energy_override

    @classmethod
    def from_render_context(
        cls,
        rcs_dict: dict,
        events_summary: str = "",
        monthly_theme_md: str = "",
    ) -> "HorizonContext":
        """Конструктор совместимости с render-pilot-guides.py.

        Используется до полной реализации Оркестратора (WP-203).
        Горизонты quarter/week/day остаются пустыми — planner.py заполнит из RCS.
        """
        rcs = RCSProfile.from_dict(rcs_dict)
        month = MonthThemes(label=(monthly_theme_md or "")[:500])
        return cls(
            rcs=rcs,
            month=month,
            summary_events=events_summary,
        )


# ─────────────────────────────────────────────────────────────────────────────
# PlanDay — выход Портного-2
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class DZItem:
    """Одно задание в дневном пакете."""
    element_id: str
    element_type: str    # worldview | mastery
    area: int            # 1–5 (FORM.081)
    target_depth: int    # глубина разбора
    tomatoes: int = 1    # ожидаемое количество помидорок
    label: str = ""      # краткое название
    rationale: str = ""  # почему именно это сегодня


@dataclass
class PlanDay:
    """Выход Портного-2 — дневной пакет ДЗ + нарратив (WP-149 Ф11).

    Заменяет dict-выход plan() для horizon-aware режима.
    Совместим: plan() возвращает dict, plan_horizon() возвращает PlanDay.
    """
    items: list[DZItem]
    narrative: str              # 1–2 абзаца «почему именно это сегодня»
    week_label: str = ""        # ISO-неделя (2026-W19)
    trigger_response: str = ""  # реакция на триггер Оркестратора
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
