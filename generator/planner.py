"""
Портной (R27) — детерминированный планировщик.
SOP MIM.SOP.001 шаги 1–4: выбор области, типа воздействия, элемента, глубины.

Входной контракт: PD.SPEC.001
Выходной контракт: LessonPlan — передаётся в prompt.md (шаги 5–6)

guide-kit fork (WP-483 Ф1): портативная копия agents/tailor/planner.py.
Логика выбора не менялась — вырезаны только platform-specific импорт логирования
и хардкод пути к DS-principles-curriculum (см. GUIDE_KIT_CURRICULUM_PATH ниже).
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from horizons import HorizonContext

# SLOT_LABELS импортируется лениво в plan_horizon() чтобы не создавать
# circular import при загрузке planner без horizons (legacy путь)
try:
    from horizons import SLOT_LABELS
except ImportError:
    SLOT_LABELS: dict[str, str] = {}

# ---------------------------------------------------------------------------
# Типы входных данных (PD.SPEC.001)
# ---------------------------------------------------------------------------

ImpactType = Literal["worldview", "mastery"]
State = Literal["chaos", "stuck", "pivot", "development"]
Area = Literal[1, 2, 3, 4, 5]  # knowledge, tools, constraints, environment, organism

AREA_NAMES = {
    1: "knowledge",
    2: "tools",
    3: "constraints",
    4: "environment",
    5: "organism",
}

ROLE_AREA_BOOSTS: dict[str, tuple[int, int]] = {
    # dominant_role → (primary_area, secondary_area)
    "learner":       (1, 3),
    "intellectual":  (1, 2),
    "professional":  (2, 1),
    "researcher":    (1, 3),
    "enlightener":   (4, 1),
}

# Матрица весов: фаза (1-4) × область (1-5) — из SOP.001 § Матрица
PHASE_WEIGHTS: dict[int, list[float]] = {
    1: [1.5, 0.5, 0.8, 0.7, 1.0],
    2: [1.0, 1.0, 0.8, 0.8, 1.0],
    3: [0.8, 1.5, 0.8, 0.5, 1.4],
    4: [0.8, 1.8, 0.5, 0.8, 1.4],
}

# impact_type базовое соотношение: ступень → вероятность worldview
STAGE_WORLDVIEW_PROB: dict[int, float] = {
    1: 0.80,
    2: 0.80,
    3: 0.50,
    4: 0.50,
    5: 0.20,
}

# Нарративная дуга: ступень (0-4 в коде = 1-5 в Pack) → (narrative_phase, worldview_arc)
# Source-of-truth: PD.FORM.080 §3 + PD.FORM.087 §5. WP-245 Ф6
STAGE_NARRATIVE: dict[int, tuple[str, str]] = {
    1: ("Я могу меняться", "Я могу меняться"),                          # ст.1 Случайный
    2: ("Я — система", "Я — система"),                                  # ст.2 Практикующий
    3: ("Окружение влияет на меня", "Окружение влияет на меня"),         # ст.3 Систематический
    4: ("Мир — система", "Мир — система, и я в ней деятель"),            # ст.4 Дисциплинированный
    5: ("Мы меняем мир", "Системное мировоззрение, agency"),             # ст.5 Проактивный
}

# Каталог CAT.002: практики досуга × область (area) × entry_stage
# element_id → {area, entry_stage, name}
# name: русское имя из frontmatter карточки (DS-principles-curriculum/.../CAT.002/).
# Имя зашито в словарь, потому что на tsekh-1 репо каталогов отсутствует, а промпту
# нужно русское имя вместо голого кода (WP-149, красная ночь 2026-07-11, G5-inline-code).
CAT002_ELEMENTS = {
    "CAT.002.A1": {"area": 5, "entry_stage": 1, "name": "Сон и распорядок дня"},
    "CAT.002.A2": {"area": 5, "entry_stage": 1, "name": "Отдых между помидорками"},
    "CAT.002.A3": {"area": 5, "entry_stage": 2, "name": "Двигательная практика"},
    "CAT.002.A4": {"area": 5, "entry_stage": 2, "name": "Питание и гидратация"},
    "CAT.002.A5": {"area": 5, "entry_stage": 3, "name": "Саморегуляция"},
    "CAT.002.A6": {"area": 5, "entry_stage": 3, "name": "Медицинский чек-ап"},
    "CAT.002.B1": {"area": 5, "entry_stage": 2, "name": "Замена удовольствий"},
    "CAT.002.B2": {"area": 5, "entry_stage": 2, "name": "Микро-приключения"},
    "CAT.002.B3": {"area": 5, "entry_stage": 3, "name": "Путешествия и смена контекста"},
    "CAT.002.B4": {"area": 5, "entry_stage": 3, "name": "Фиксация впечатлений"},
}

# Каталог CAT.003: практики обучения × область × entry_stage
# Источник: DS-principles-curriculum/data/curriculum/CAT.003/
# area: 1=knowledge, 2=tools, 3=constraints, 4=environment, 5=organism
# entry_stage: минимальная ступень для доступа (1 = Случайный, доступно всем)
# name: русское имя из frontmatter карточки (см. комментарий у CAT002_ELEMENTS)
CAT003_ELEMENTS: dict[str, dict] = {
    "CAT.003.METHOD.001": {"area": 2, "entry_stage": 1, "name": "Инвестирование и учёт времени"},
    "CAT.003.METHOD.003": {"area": 1, "entry_stage": 1, "name": "Систематическое медленное чтение"},
    "CAT.003.METHOD.004": {"area": 1, "entry_stage": 1, "name": "Мышление письмом"},
    "CAT.003.METHOD.005": {"area": 1, "entry_stage": 1, "name": "Мышление проговариванием"},
    "CAT.003.METHOD.006": {"area": 5, "entry_stage": 1, "name": "Организация досуга"},
    "CAT.003.METHOD.007": {"area": 4, "entry_stage": 1, "name": "Формирование окружения"},
    "CAT.003.METHOD.008": {"area": 1, "entry_stage": 2, "name": "Стратегирование"},  # Практикующий+
    "CAT.003.METHOD.009": {"area": 2, "entry_stage": 1, "name": "Планирование"},
}

# Каталог CAT.001: мировоззренческие мемы × область × entry_stage
# Структура: element_id → {area, entry_stage, max_depth=3}
# Источник (платформенный): DS-principles-curriculum/data/curriculum/CAT.001/
# guide-kit: путь не хардкодится — задаётся через GUIDE_KIT_CURRICULUM_PATH
# (guide-kit.config.yaml → curriculum_path). Не задан → честный пустой индекс,
# prompt.md выбирает мировоззренческий элемент самостоятельно (см. _load_cat001).
# Загружается из файловой системы при первом обращении (lazy load).

_CAT001_CACHE: dict[str, dict] | None = None


def _load_cat001() -> dict[str, dict]:
    """Читает frontmatter из M-*.md файлов CAT.001, строит индекс."""
    global _CAT001_CACHE
    if _CAT001_CACHE is not None:
        return _CAT001_CACHE

    result: dict[str, dict] = {}
    curriculum_path = os.environ.get("GUIDE_KIT_CURRICULUM_PATH", "")

    if not curriculum_path or not os.path.isdir(curriculum_path):
        # Нет курикулы (портативный профиль без платформы) — честный пустой индекс,
        # prompt.md fallback выбирает мировоззренческий элемент самостоятельно.
        _CAT001_CACHE = result
        return result

    cat001_dir = os.path.normpath(curriculum_path)

    frontmatter_re = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)

    for fname in os.listdir(cat001_dir):
        if not (fname.startswith("M-") and fname.endswith(".md")):
            continue
        fpath = os.path.join(cat001_dir, fname)
        try:
            with open(fpath, encoding="utf-8") as fh:
                content = fh.read()
        except OSError:
            continue

        m = frontmatter_re.match(content)
        if not m:
            continue

        fm: dict = {}
        for line in m.group(1).splitlines():
            if ":" in line:
                key, _, val = line.partition(":")
                fm[key.strip()] = val.strip().strip('"').strip("'")

        element_id = fm.get("id")
        try:
            area = int(fm["area"])
            entry_stage = int(fm.get("entry_stage", 1))
        except (KeyError, ValueError):
            continue

        # context: 1=Саморазвитие, 2=Работа, 3=Досуг (FORM.082)
        context_str = fm.get("context", "")
        context_map = {"Саморазвитие": 1, "Работа": 2, "Досуг": 3}
        context_val = context_map.get(context_str, 0)

        if element_id and area in range(1, 6):
            result[element_id] = {
                "area": area,
                "entry_stage": entry_stage,
                "max_depth": 3,
                "context": context_val,
                "name": fm.get("name", ""),
            }

    _CAT001_CACHE = result
    return result


# Lazy accessor — используется в _choose_element_worldview
def _get_cat001() -> dict[str, dict]:
    return _load_cat001()


def element_name(element_id: str) -> str:
    """Русское имя элемента каталога для текста промпта, "" если неизвестно.

    WP-149 (красная ночь 2026-07-11): голый код элемента (CAT.003.METHOD.003)
    инжектировался в промпт и просачивался в текст урока — G5-inline-code.
    Генератор подставляет имя вместо кода; код остаётся только в decision_log.
    """
    for catalog in (CAT003_ELEMENTS, CAT002_ELEMENTS, _get_cat001()):
        entry = catalog.get(element_id)
        if entry and entry.get("name"):
            return entry["name"]
    return ""


# ---------------------------------------------------------------------------
# Контекст FORM.082: вывод и подсказка
# ---------------------------------------------------------------------------

# Маппинг каталог → контекст (для CAT.002/CAT.003 и fallback)
_CATALOG_CONTEXT: dict[str, int] = {
    "CAT.002": 3,   # Досуг
    "CAT.003": 1,   # Саморазвитие
    "DP.M.008": 2,  # Работа
}

_CONTEXT_NAMES = {1: "Саморазвитие", 2: "Работа", 3: "Досуг"}

_CONTEXT_HINTS = {
    1: "Выделите 20 минут в своём учебном слоте",
    2: "Применяйте при следующей работе над рабочим продуктом",
    3: "Встройте в досуг на ближайшей неделе",
}


def _derive_context(element_id: str | None) -> int:
    """Вывести контекст из element_id (FORM.008 §3).

    CAT.001 → из frontmatter (поле context в кэше).
    CAT.002 → 3 (Досуг). CAT.003 → 1 (Саморазвитие). DP.M.008 → 2 (Работа).
    Fallback → 1 (Саморазвитие).
    """
    if not element_id:
        return 1

    # CAT.001 — из кэша frontmatter
    if element_id.startswith("CAT.001"):
        cat001 = _get_cat001()
        meta = cat001.get(element_id)
        if meta and meta.get("context"):
            return meta["context"]
        return 1  # fallback

    # Другие каталоги — по префиксу
    for prefix, ctx in _CATALOG_CONTEXT.items():
        if element_id.startswith(prefix):
            return ctx

    return 1


def _build_context_hint(context: int, student_stage: int) -> str:
    """Подсказка когда/где выполнять задание (FORM.008 §3)."""
    return _CONTEXT_HINTS.get(context, _CONTEXT_HINTS[1])


CAT001_ELEMENTS: dict[str, dict] = {}  # legacy alias; реальные данные через _get_cat001()


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class RecentLesson:
    element_id: str
    element_type: str  # "worldview" | "mastery"
    area: int
    depth: int
    passed: bool
    errors: list[str] = field(default_factory=list)
    rating: int | None = None
    date: str | None = None


@dataclass
class TailorContext:
    """Входной контракт (PD.SPEC.001)."""
    student_stage: int                     # 0–4
    it_level: int                          # 0–3
    dominant_role: str                     # learner | intellectual | professional | researcher | enlightener
    state: State                           # chaos | stuck | pivot | development
    energy: int                            # 1–5
    phase: int                             # 1–4 (из SOP шага 2, или вычисляется по ступени)
    mastery_by_area: dict[str, int]        # {knowledge: N, tools: N, ...} — N = текущая глубина
    last_area: int | None                  # область последнего занятия (1–5)
    recent_history: list[RecentLesson]
    worldview_gaps: list[str]              # [] если L3 не вычислен
    mastery_gaps: list[str]               # [] если L3 не вычислен
    domain: str                            # профессиональный домен ученика

    @classmethod
    def from_dict(cls, d: dict) -> "TailorContext":
        history = [
            RecentLesson(**r) if isinstance(r, dict) else r
            for r in d.get("recent_history", [])
        ]
        return cls(
            student_stage=int(d["student_stage"]),
            it_level=int(d["it_level"]),
            dominant_role=str(d["dominant_role"]),
            state=d["state"],
            energy=int(d["energy"]),
            phase=int(d.get("phase") or _stage_to_phase(int(d["student_stage"]))),
            mastery_by_area=d.get("mastery_by_area", {}),
            last_area=d.get("last_area"),
            recent_history=history,
            worldview_gaps=d.get("worldview_gaps", []),
            mastery_gaps=d.get("mastery_gaps", []),
            domain=str(d.get("domain", "")),
        )


@dataclass
class LessonPlan:
    """Выходной контракт plannerа → вход prompt.md (шаги 5–6)."""
    area: int                      # 1–5
    element_id: str               # CAT.001.A3, CAT.002.B1, ...
    element_type: str             # "worldview" | "mastery"
    impact_type: ImpactType
    target_depth: int             # 1–4
    session_goal: str             # сформулированная цель занятия
    decision_log: dict            # аудит-след


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def _stage_to_phase(stage: int) -> int:
    """SOP шаг 2: ступень → фаза."""
    return {0: 1, 1: 1, 2: 2, 3: 3, 4: 4}.get(stage, 1)


def _compute_area_weights(ctx: TailorContext) -> list[float]:
    """
    SOP шаг 3: вычислить веса областей.
    Возвращает список из 5 весов [w1, w2, w3, w4, w5].
    """
    weights = list(PHASE_WEIGHTS[ctx.phase])  # копия

    # Корректировка по состоянию
    state_adjustments: dict[str, dict[int, float]] = {
        "chaos":       {5: 2.0, 1: 0.5},
        "stuck":       {3: 1.5, 1: 1.5},
        "pivot":       {},
        "development": {2: 1.5},
    }
    for area_idx, multiplier in state_adjustments.get(ctx.state, {}).items():
        weights[area_idx - 1] *= multiplier

    # Корректировка по энергии
    if ctx.energy <= 2:
        weights[4] *= 1.5  # область 5 (Организм) — индекс 4

    # Корректировка по доминирующей роли
    role_areas = ROLE_AREA_BOOSTS.get(ctx.dominant_role)
    if role_areas:
        for area_idx in role_areas:
            weights[area_idx - 1] *= 1.5

    # Ротация: обнулить вчерашнюю область
    if ctx.last_area is not None:
        weights[ctx.last_area - 1] = 0.0

    return weights


def _choose_area(weights: list[float], mastery_by_area: dict[str, int]) -> tuple[int, str]:
    """
    SOP шаг 3: выбрать область.
    Критерий: max(вес × gap), где gap = 1 если глубина < 4 иначе 0.
    Возвращает (area_int, reason_str).
    """
    area_keys = ["knowledge", "tools", "constraints", "environment", "organism"]
    scores = []
    for i, key in enumerate(area_keys):
        current_depth = mastery_by_area.get(key, 0)
        gap = max(0, 4 - current_depth)
        score = weights[i] * gap
        scores.append(score)

    total = sum(scores)
    if total == 0:
        # Все области на максимуме — fallback: берём первую с ненулевым весом
        for i, w in enumerate(weights):
            if w > 0:
                chosen = i + 1
                return chosen, f"fallback: все области max depth, первая с ненулевым весом: {AREA_NAMES[chosen]}"
        return 1, "fallback: все веса нулевые, выбрана область 1"

    chosen_idx = scores.index(max(scores))
    chosen_area = chosen_idx + 1
    reason = (
        f"area={chosen_area} ({AREA_NAMES[chosen_area]}): "
        f"score={scores[chosen_idx]:.2f} "
        f"(weight={weights[chosen_idx]:.2f} × gap={scores[chosen_idx]/weights[chosen_idx]:.0f})"
        if weights[chosen_idx] > 0 else
        f"area={chosen_area}: выбран как максимальный score"
    )
    return chosen_area, reason


def _choose_impact_type(ctx: TailorContext) -> tuple[ImpactType, str]:
    """
    SOP шаг 3: выбор типа воздействия.
    Базовое соотношение по ступени + корректировка по GAP если есть.
    """
    worldview_prob = STAGE_WORLDVIEW_PROB.get(ctx.student_stage, 0.5)

    # Корректировка по GAP-отчёту
    reason = f"base_prob_worldview={worldview_prob:.0%} (stage={ctx.student_stage})"
    if ctx.worldview_gaps or ctx.mastery_gaps:
        wv_gap_count = len(ctx.worldview_gaps)
        ms_gap_count = len(ctx.mastery_gaps)
        if wv_gap_count > ms_gap_count:
            worldview_prob = min(1.0, worldview_prob + 0.2)
            reason += f"; GAP-корректировка: worldview_gaps({wv_gap_count}) > mastery_gaps({ms_gap_count}) → +0.2"
        elif ms_gap_count > wv_gap_count:
            worldview_prob = max(0.0, worldview_prob - 0.2)
            reason += f"; GAP-корректировка: mastery_gaps({ms_gap_count}) > worldview_gaps({wv_gap_count}) → -0.2"
        else:
            reason += "; GAP-отчёт: равные пробелы → без корректировки"
    else:
        reason += "; GAP-отчёт: нет (L3 не вычислен) → weighted random"

    impact = "worldview" if random.random() < worldview_prob else "mastery"
    reason += f" → выбран {impact}"
    return impact, reason


def _get_recent_element_ids(recent: list[RecentLesson], n: int = 5) -> set[str]:
    return {r.element_id for r in recent[:n]}


def _choose_element_worldview(
    area: int,
    stage: int,
    recent_ids: set[str],
    worldview_gaps: list[str],
    mastery_by_area: dict[str, int],
    recent_history: list | None = None,
) -> tuple[str | None, int, str]:
    """
    SOP шаг 4: выбрать мем из CAT.001.
    Возвращает (element_id, target_depth, reason) или (None, 1, reason) если нет элементов.

    Приоритеты:
    1. worldview_gaps (от Диагноста) — если есть и не в recent
    2. CAT.001 из файловой системы — bottleneck-first по глубине
    3. Fallback → prompt.md
    """
    history = recent_history or []

    # 1. GAP-отчёт от Диагноста
    if worldview_gaps:
        candidates = [e for e in worldview_gaps if e not in recent_ids]
        if candidates:
            chosen = candidates[0]
            current_depth = _get_current_depth(chosen, history)
            target = min(current_depth + 1, 3)
            return chosen, target, f"worldview_gaps → {chosen}, depth {current_depth}→{target}"
        chosen = worldview_gaps[0]
        current_depth = _get_current_depth(chosen, history)
        target = min(current_depth + 1, 3)
        return chosen, target, f"worldview_gaps (все recent) → {chosen}, depth {current_depth}→{target}"

    # 2. Загрузить каталог из файловой системы
    cat001 = _get_cat001()
    if not cat001:
        return None, 1, "нет GAP-отчёта и CAT001 не загружен → prompt.md выбирает из каталога"

    # Фильтр по area и доступности по ступени
    candidates_pool = {
        eid: meta
        for eid, meta in cat001.items()
        if meta["area"] == area and meta["entry_stage"] <= stage
    }

    if not candidates_pool:
        # Расширяем поиск: любая область
        candidates_pool = {
            eid: meta
            for eid, meta in cat001.items()
            if meta["entry_stage"] <= stage
        }
        if not candidates_pool:
            return None, 1, f"CAT001: нет доступных мемов для stage={stage}"

    # Исключить recent, если есть что-то за их пределами
    non_recent = {eid: m for eid, m in candidates_pool.items() if eid not in recent_ids}
    pool = non_recent if non_recent else candidates_pool

    # Bottleneck-first: выбрать мем с наибольшим gap (max_depth - current_depth)
    best_eid = None
    best_gap = -1
    for eid in pool:
        current_depth = _get_current_depth(eid, history)
        max_depth = pool[eid].get("max_depth", 3)
        gap = max_depth - current_depth
        if gap > best_gap:
            best_gap = gap
            best_eid = eid

    if best_eid is None:
        return None, 1, "CAT001: bottleneck-first не нашёл кандидата"

    current_depth = _get_current_depth(best_eid, history)
    target = min(current_depth + 1, 3)
    reason = (
        f"CAT001 bottleneck-first → {best_eid} "
        f"(area={candidates_pool.get(best_eid, pool[best_eid])['area']}, "
        f"gap={best_gap}, depth {current_depth}→{target})"
    )
    return best_eid, target, reason


def _choose_element_mastery(
    area: int,
    stage: int,
    recent_ids: set[str],
    mastery_gaps: list[str],
    mastery_by_area: dict[str, int],
    recent_history: list[RecentLesson],
) -> tuple[str | None, int, str]:
    """
    SOP шаг 4: выбрать практику из CAT.002 или CAT.003.
    Возвращает (element_id, target_depth, reason).
    """
    # Объединяем CAT.002 и CAT.003
    all_mastery = {**CAT002_ELEMENTS, **CAT003_ELEMENTS}

    # Фильтр по области и доступности по ступени
    candidates = {
        eid: meta
        for eid, meta in all_mastery.items()
        if meta["area"] == area and meta["entry_stage"] <= stage
    }

    if not candidates:
        # Fallback: другая область → prompt.md обработает
        return None, 1, f"нет mastery-элементов для area={area}, stage={stage} → prompt.md fallback"

    # Исключить recent_ids
    non_recent = {eid: meta for eid, meta in candidates.items() if eid not in recent_ids}
    pool = non_recent if non_recent else candidates  # если все recent — возвращаем все

    # Если есть mastery_gaps — приоритет им
    if mastery_gaps:
        gap_candidates = [e for e in mastery_gaps if e in pool]
        if gap_candidates:
            chosen = gap_candidates[0]
            # Определить текущую глубину из recent_history
            current_depth = _get_current_depth(chosen, recent_history)
            target = current_depth + 1
            return chosen, target, f"mastery_gaps → {chosen}, depth {current_depth}→{target}"

    # Bottleneck-first: найти элемент с наибольшим gap
    # История ошибок: приоритет элементам с ошибками при равном gap
    history_map = {r.element_id: r for r in recent_history}

    best_eid = None
    best_gap = -1
    best_has_errors = False

    for eid in pool:
        current = _get_current_depth(eid, recent_history)
        target_max = 4  # max degree для CAT.002/003
        gap = target_max - current
        has_errors = len(history_map.get(eid, RecentLesson(eid, "", 0, 0, False)).errors) > 0

        better = (
            gap > best_gap
            or (gap == best_gap and has_errors and not best_has_errors)
        )
        if better:
            best_eid = eid
            best_gap = gap
            best_has_errors = has_errors

    if best_eid is None:
        return None, 1, "нет подходящих mastery-элементов"

    current_depth = _get_current_depth(best_eid, recent_history)
    target = current_depth + 1
    reason = f"bottleneck-first → {best_eid}, gap={best_gap}, errors={best_has_errors}, depth {current_depth}→{target}"
    return best_eid, target, reason


def _get_current_depth(element_id: str, history: list[RecentLesson]) -> int:
    """Найти максимальную пройденную глубину для элемента в истории."""
    passed = [
        r.depth for r in history
        if r.element_id == element_id and r.passed
    ]
    return max(passed) if passed else 0


def _mastery_gate(element_id: str, target_depth: int, history: list[RecentLesson]) -> tuple[int, str]:
    """
    SOP шаг 4c: mastery-gate — не повышать глубину без прохождения can-do.
    Возвращает (actual_depth, reason).
    """
    if target_depth <= 1:
        return 1, "новый элемент → depth=1"

    previous_depth = target_depth - 1
    can_do_passed = any(
        r.element_id == element_id and r.depth == previous_depth and r.passed
        for r in history
    )

    if can_do_passed:
        return target_depth, f"mastery-gate ✓: depth {previous_depth} пройден → повышаем до {target_depth}"
    else:
        return previous_depth, f"mastery-gate ✗: depth {previous_depth} НЕ пройден → остаёмся на {previous_depth}"


def _build_session_goal(element_id: str | None, impact_type: ImpactType, area: int, depth: int) -> str:
    area_name = AREA_NAMES.get(area, str(area))
    if impact_type == "worldview":
        return f"Переосмыслить мировоззренческий паттерн в области «{area_name}» (глубина {depth})"
    else:
        return f"Освоить практику в области «{area_name}»: {element_id or 'по каталогу'} (степень {depth})"


# ---------------------------------------------------------------------------
# Основная функция
# ---------------------------------------------------------------------------

def plan(tailor_context: dict, seed: int | None = None) -> dict:
    """
    Детерминированный планировщик: SOP.001 шаги 1–4.

    Args:
        tailor_context: словарь по PD.SPEC.001
        seed: зафиксировать random для воспроизводимости (тесты)

    Returns:
        dict с полями LessonPlan + decision_log
    """
    if seed is not None:
        random.seed(seed)

    # --- Шаг 1: Валидация и разбор входа ---
    try:
        ctx = TailorContext.from_dict(tailor_context)
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"Невалидный tailor_context (PD.SPEC.001): {e}") from e

    # Санитизация user-generated полей
    safe_domain = ctx.domain[:200].replace("\n", " ").replace("\r", "")
    safe_state = ctx.state if ctx.state in ("chaos", "stuck", "pivot", "development") else "development"
    ctx.state = safe_state

    # --- Шаг 2: Фаза (уже в TailorContext.from_dict) ---
    phase = ctx.phase

    # --- Шаг 3: Веса, область, тип воздействия ---
    weights = _compute_area_weights(ctx)
    area, area_reason = _choose_area(weights, ctx.mastery_by_area)
    impact_type, impact_reason = _choose_impact_type(ctx)

    # --- Шаг 4: Элемент и глубина ---
    recent_ids = _get_recent_element_ids(ctx.recent_history)

    if impact_type == "worldview":
        element_id, raw_depth, element_reason = _choose_element_worldview(
            area, ctx.student_stage, recent_ids,
            ctx.worldview_gaps, ctx.mastery_by_area,
            ctx.recent_history,
        )
    else:
        element_id, raw_depth, element_reason = _choose_element_mastery(
            area, ctx.student_stage, recent_ids,
            ctx.mastery_gaps, ctx.mastery_by_area, ctx.recent_history,
        )

    # Mastery-gate
    if element_id:
        target_depth, gate_reason = _mastery_gate(element_id, raw_depth, ctx.recent_history)
    else:
        target_depth = 1
        gate_reason = "элемент не выбран — prompt.md выбирает самостоятельно"

    # Fallback: нет элементов → переключить impact_type
    if element_id is None and impact_type == "worldview":
        fallback_note = "worldview → нет элементов → fallback mastery"
        impact_type = "mastery"
        element_id, raw_depth, element_reason = _choose_element_mastery(
            area, ctx.student_stage, recent_ids,
            ctx.mastery_gaps, ctx.mastery_by_area, ctx.recent_history,
        )
        target_depth = raw_depth
        gate_reason += f"; {fallback_note}"
    elif element_id is None and impact_type == "mastery":
        fallback_note = "mastery → нет элементов → fallback worldview"
        impact_type = "worldview"
        element_id, raw_depth, element_reason = _choose_element_worldview(
            area, ctx.student_stage, recent_ids,
            ctx.worldview_gaps, ctx.mastery_by_area,
            ctx.recent_history,
        )
        target_depth = raw_depth
        gate_reason += f"; {fallback_note}"

    element_type = "worldview" if impact_type == "worldview" else "mastery"
    session_goal = _build_session_goal(element_id, impact_type, area, target_depth)

    # Контекст FORM.082
    context_code = _derive_context(element_id)
    context_hint = _build_context_hint(context_code, ctx.student_stage)

    decision_log = {
        "area_choice": area_reason,
        "element_choice": element_reason,
        "impact_type_choice": impact_reason,
        "depth_rationale": gate_reason,
        "context": f"{context_code} ({_CONTEXT_NAMES.get(context_code, '?')})",
        "phase": phase,
        "weights": {AREA_NAMES[i + 1]: round(w, 3) for i, w in enumerate(weights)},
    }

    return {
        "lesson_plan": {
            "area": area,
            "element_id": element_id,
            "element_type": element_type,
            "impact_type": impact_type,
            "target_depth": target_depth,
            "session_goal": session_goal,
            "context": context_code,
            "context_hint": context_hint,
        },
        "decision_log": decision_log,
        # Мета для prompt.md
        "context_for_llm": {
            "student_stage": ctx.student_stage,
            "it_level": ctx.it_level,
            "state": safe_state,
            "energy": ctx.energy,
            "dominant_role": ctx.dominant_role,
            "domain": safe_domain,
            "narrative_phase": STAGE_NARRATIVE.get(ctx.student_stage, ("Я — система", "Я — система"))[0],
            "worldview_arc": STAGE_NARRATIVE.get(ctx.student_stage, ("Я — система", "Я — система"))[1],
            "recent_history": [
                {
                    "element_id": r.element_id,
                    "area": r.area,
                    "depth": r.depth,
                    "passed": r.passed,
                    # errors санитизируется: только первые 5 по 100 символов
                    "errors": [str(e)[:100] for e in (r.errors or [])[:5]],
                }
                for r in ctx.recent_history[:5]
            ],
        },
    }


# ---------------------------------------------------------------------------
# Портной-2: plan_horizon() — horizon-aware планировщик (WP-149 Ф9)
# ---------------------------------------------------------------------------

# Маппинг RCS-слот → область FORM.081 и тип воздействия
# Источник: PD.FORM.089 + SOP.001 матрица весов
_SLOT_TO_AREA: dict[str, int] = {
    "W":  1,  # knowledge — мировоззрение через концепции
    "M1": 3,  # constraints — фокус/собранность как ограничение на attention
    "M2": 2,  # tools — IWE/ОРЗ как инструмент
    "M3": 1,  # knowledge — доменные знания
    "M4": 1,  # knowledge — системное мышление
    "IT": 2,  # tools — ИТ-инструменты
    "A":  4,  # environment — агентность через окружение и связи
}

_SLOT_TO_IMPACT: dict[str, ImpactType] = {
    "W":  "worldview",  # мировоззрение — мемы CAT.001
    "M1": "mastery",    # фокус — практики CAT.003
    "M2": "mastery",    # IWE — практики CAT.003
    "M3": "mastery",    # домен — практики CAT.003
    "M4": "worldview",  # системное мышление → мировоззрение (мемы CAT.001)
    "IT": "mastery",    # ИТ-уровень — практики
    "A":  "worldview",  # агентность → мировоззрение
}

# Энергия/триггер → количество помидорок в день
_TRIGGER_TOMATOES: dict[str, int] = {
    "slot_miss": 1,
    "calendar_event": 1,
    "routine": 2,
    "focus_shift": 2,
    "metric_jump": 2,
    "blocker": 1,
    "hypothesis_fail": 2,
}


def _rcs_to_tailor_context_dict(ctx: "HorizonContext") -> dict:
    """Конвертирует HorizonContext в совместимый dict для plan().

    Используется как промежуточный шаг: выбираем область и элемент
    через существующую логику plan(), но с RCS-based параметрами.
    """
    from horizons import HorizonContext as HC
    rcs = ctx.rcs
    # Маппинг RCS stage_derived (1-5 Pack) → student_stage (0-4 код)
    stage_code = max(0, min(4, rcs.stage_derived - 1))

    # Bottleneck → state
    bottleneck = ctx.effective_bottleneck()
    if ctx.trigger.kind == "slot_miss":
        state = "chaos"
    elif ctx.trigger.kind == "blocker":
        state = "stuck"
    elif ctx.trigger.kind == "focus_shift":
        state = "pivot"
    else:
        state = "development"

    # Энергия: override от Оркестратора или дефолт по trigger
    energy = ctx.day.energy_override or (1 if ctx.trigger.kind == "slot_miss" else 3)

    # GAP-списки: приоритет месячным темам из HorizonContext
    worldview_gaps = list(ctx.month.memes) if ctx.month.memes else []
    mastery_gaps = list(ctx.month.methods) if ctx.month.methods else []

    return {
        "student_stage": stage_code,
        "it_level": rcs.IT,
        "dominant_role": "learner",   # нет в RCS — дефолт
        "state": state,
        "energy": energy,
        "mastery_by_area": {},
        "last_area": None,
        "recent_history": [],
        "worldview_gaps": worldview_gaps,
        "mastery_gaps": mastery_gaps,
        "domain": "",
    }


def plan_horizon(ctx: "HorizonContext", seed: int | None = None) -> dict:
    """Портной-2: horizon-aware планировщик (WP-149 Ф9).

    Вход: HorizonContext (RCS + 4 горизонта + триггер)
    Выход: dict с ключами {mode, plan_skeleton, horizon_context, context_for_llm,
    decision_log} для prompt.md → LLM собирает PlanDay (тип в horizons.py).
    `PlanDay` намеренно не возвращается из planner — он остаётся выходным
    контрактом LLM-стадии (шаги 5-6 SOP), а planner отвечает только за каркас.

    Отличия от plan():
    - Выбор области и impact_type от RCS bottleneck (не от stage weights)
    - Месячные темы → worldview_gaps / mastery_gaps (приоритет элементов)
    - Триггер Оркестратора → state, energy, tomatoes
    - Каскад горизонтов явно передаётся LLM для нарратива

    mastery_by_area берётся из ctx.mastery_by_area (Ф4.1, WP-203).
    recent_history остаётся [] — конвертация domain_event → RecentLesson отложена.
    """
    from horizons import HorizonContext

    if seed is not None:
        random.seed(seed)

    rcs = ctx.rcs
    bottleneck = ctx.effective_bottleneck()

    # Определяем область и impact_type по bottleneck
    primary_area = _SLOT_TO_AREA.get(bottleneck, 1)
    impact_type = _SLOT_TO_IMPACT.get(bottleneck, "worldview")

    # Если week задаёт focus_area — переопределяем
    if ctx.week.focus_area:
        primary_area = ctx.week.focus_area

    stage_code = max(0, min(4, rcs.stage_derived - 1))

    # GAP-приоритеты из месячных тем
    worldview_gaps = list(ctx.month.memes) if ctx.month.memes else []
    mastery_gaps = list(ctx.month.methods) if ctx.month.methods else []

    # Выбор элемента через существующие функции planner
    # mastery_by_area из Память.Derived (Ф4.1, WP-203) — активирует _mastery_gate
    mastery_by_area = getattr(ctx, "mastery_by_area", {}) or {}
    recent_ids: set[str] = set()
    if impact_type == "worldview":
        element_id, raw_depth, element_reason = _choose_element_worldview(
            primary_area, stage_code, recent_ids, worldview_gaps, mastery_by_area, []
        )
        if element_id is None:
            impact_type = "mastery"
            element_id, raw_depth, element_reason = _choose_element_mastery(
                primary_area, stage_code, recent_ids, mastery_gaps, mastery_by_area, []
            )
            if element_id is None:
                # двусторонний fallback: mastery тоже пуст → возврат к worldview
                impact_type = "worldview"
                element_id, raw_depth, element_reason = _choose_element_worldview(
                    primary_area, stage_code, recent_ids, worldview_gaps, mastery_by_area, []
                )
    else:
        element_id, raw_depth, element_reason = _choose_element_mastery(
            primary_area, stage_code, recent_ids, mastery_gaps, mastery_by_area, []
        )
        if element_id is None:
            impact_type = "worldview"
            element_id, raw_depth, element_reason = _choose_element_worldview(
                primary_area, stage_code, recent_ids, worldview_gaps, mastery_by_area, []
            )
            if element_id is None:
                # двусторонний fallback: worldview тоже пуст → возврат к mastery
                impact_type = "mastery"
                element_id, raw_depth, element_reason = _choose_element_mastery(
                    primary_area, stage_code, recent_ids, mastery_gaps, mastery_by_area, []
                )

    # Mastery-gate: не повышать глубину без прохождения can-do (P5 fix)
    if element_id:
        target_depth, gate_reason = _mastery_gate(element_id, raw_depth, [])
    else:
        target_depth = 1
        gate_reason = "элемент не выбран — prompt.md выбирает самостоятельно"

    # Помидорки: по триггеру + энергии
    base_tomatoes = _TRIGGER_TOMATOES.get(ctx.trigger.kind, 2)
    energy = ctx.energy()
    if energy <= 2 or ctx.day.calendar_load == "heavy":
        tomatoes = 1
    elif energy >= 4 and base_tomatoes >= 2:
        tomatoes = 2
    else:
        tomatoes = base_tomatoes

    # Нарратив: какую фазу использовать
    narrative_phase = STAGE_NARRATIVE.get(stage_code, ("Я — система", "Я — система"))

    # Форматируем горизонты для LLM
    quarter_block = {
        "bottleneck_slot": ctx.quarter.bottleneck_slot or bottleneck,
        "theme": ctx.quarter.theme,
        "target_delta": ctx.quarter.target_delta,
    }
    month_block = {
        "memes": ctx.month.memes,
        "methods": ctx.month.methods,
        "label": ctx.month.label,
    }
    week_block = {
        "expected_delta": ctx.week.expected_delta,
        "slack_budget": ctx.week.slack_budget,
        "focus_area": ctx.week.focus_area,
        "label": ctx.week.label,
    }
    day_block = {
        "missed_slots": ctx.day.missed_slots,
        "calendar_load": ctx.day.calendar_load,
        "energy": energy,
        "notes": ctx.day.notes,
    }

    decision_log = {
        "bottleneck": bottleneck,
        "primary_area": f"{primary_area} ({AREA_NAMES.get(primary_area, '?')})",
        "impact_type": impact_type,
        "element_choice": element_reason,
        "target_depth": target_depth,
        "depth_rationale": gate_reason,
        "tomatoes": tomatoes,
        "trigger": f"{ctx.trigger.kind}: {ctx.trigger.detail}",
        "rcs_stage": rcs.stage_derived,
    }

    return {
        "mode": "horizon",
        "plan_skeleton": {
            "element_id": element_id,
            "element_type": "worldview" if impact_type == "worldview" else "mastery",
            "area": primary_area,
            "target_depth": target_depth,
            "tomatoes": tomatoes,
        },
        "horizon_context": {
            "quarter": quarter_block,
            "month": month_block,
            "week": week_block,
            "day": day_block,
            "artifacts_summary": {
                "count": ctx.artifacts.count,
                "by_type": ctx.artifacts.by_type,
                "recent_titles": ctx.artifacts.recent_titles[:5],
            },
            "summary_events": ctx.summary_events,
            "pilot_reflection": ctx.pilot_reflection,
            "reflection_learned": ctx.reflection_learned,
            "tomorrow_intention": ctx.tomorrow_intention,
        },
        "context_for_llm": {
            "rcs": rcs.to_dict(),
            "stage_derived": rcs.stage_derived,
            "it_level": rcs.IT,
            "narrative_phase": narrative_phase[0],
            "worldview_arc": narrative_phase[1],
            "bottleneck_slot": bottleneck,
            "bottleneck_label": SLOT_LABELS.get(bottleneck, bottleneck),
        },
        "decision_log": decision_log,
    }


# ---------------------------------------------------------------------------
# CLI-запуск (headless)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Читаем tailor_context из stdin (JSON), выводим lesson_plan в stdout (JSON)
    raw = sys.stdin.read()
    try:
        context = json.loads(raw)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"Невалидный JSON на stdin: {e}"}))
        sys.exit(1)

    try:
        result = plan(context)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except ValueError as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
