"""
adapter.py — guide-kit generator adapter (WP-483 Ф1).

Bridges a user's profile.yaml (WP-476 axes 2.1-2.4) to the deterministic
planner (planner.py + horizons.py) and a configurable LLM backend, producing
either a markdown plan or a diagnostic failure report — never a silently
invented fact (hard-fail policy, see policies/default.yaml).

CLI:
    python3 adapter.py --profile profile.yaml [--config guide-kit.config.yaml]
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import yaml

from horizons import (
    ArtifactsSummary,
    DayEvents,
    HorizonContext,
    MonthThemes,
    OrchestratorTrigger,
    QuarterFocus,
    RCSProfile,
    WeekHypothesis,
)
from planner import plan_horizon
from llm_backends import GenerationContext, PromptSpec, generate as llm_generate

logger = logging.getLogger(__name__)

_GENERATOR_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PROMPT_PATH = os.path.join(_GENERATOR_DIR, "prompt.md")
DEFAULT_POLICY_PATH = os.path.join(_GENERATOR_DIR, "policies", "default.yaml")


# ---------------------------------------------------------------------------
# Config / profile / policy loading — all tolerant of missing files
# ---------------------------------------------------------------------------

def _read_yaml(path: str) -> dict:
    """Читает YAML-файл. Синтаксически битый файл — та же партиальная-толерантность,
    что и отсутствующий: лог + пусто, не крах (WP-483 Ф1, ревью после implementation)."""
    try:
        with open(path, encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except yaml.YAMLError as e:
        logger.error("malformed YAML at %r: %s — treating as empty", path, e)
        return {}


def load_config(config_path: str | None) -> dict:
    """Загружает guide-kit.config.yaml. Отсутствие файла — не ошибка: все поля опциональны."""
    if not config_path or not os.path.isfile(config_path):
        logger.info("no config at %r — using defaults (no curriculum, anthropic backend)", config_path)
        return {}
    return _read_yaml(config_path)


def load_policy(policy_path: str | None) -> dict:
    """Загружает hard-fail policy.

    В отличие от config/profile отсутствие файла — НЕ безопасный дефолт: пустая
    policy означает "обязательных слотов нет", то есть гейт пропустит любой
    результат. Явная ошибка лучше тихого отключения защиты от выдумки фактов.
    """
    path = policy_path or DEFAULT_POLICY_PATH
    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"hard-fail policy not found at {path!r} — refusing to run with an implicit "
            f"empty policy (that would silently disable the no-invented-facts gate)"
        )
    return _read_yaml(path)


def load_profile(profile_path: str) -> dict:
    """Толерантен к отсутствию/частичному профилю — пустой профиль = корректное холодное состояние."""
    if not os.path.isfile(profile_path):
        logger.info("no profile at %r — cold start (empty profile)", profile_path)
        return {}
    return _read_yaml(profile_path)


def load_card_content(element_id: str | None, cards_path: str | None) -> dict | None:
    """Ищет карточку по element_id в локальном cards_path. Нет пути/файла/битый JSON → None (честно)."""
    if not element_id or not cards_path:
        return None
    candidate = os.path.join(cards_path, f"{element_id}.json")
    if not os.path.isfile(candidate):
        return None
    try:
        with open(candidate, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as e:
        logger.error("malformed card content at %r: %s — treating as absent", candidate, e)
        return None


# ---------------------------------------------------------------------------
# profile.yaml → HorizonContext
# ---------------------------------------------------------------------------

def _from_dict_safe(cls, d: dict):
    """dataclass из словаря, игнорируя незнакомые ключи — партиальный профиль не должен падать."""
    known = {f.name for f in dataclasses.fields(cls)}
    return cls(**{k: v for k, v in d.items() if k in known})


def build_horizon_context(profile: dict) -> HorizonContext:
    """profile.yaml (2.1-2.4, WP-476) → HorizonContext. Пустой профиль → RCSProfile()+пустые горизонты."""
    rcs_dict = profile.get("rcs") or {}
    rcs = RCSProfile.from_dict(rcs_dict) if rcs_dict else RCSProfile()
    trigger_dict = profile.get("trigger") or {}
    trigger = _from_dict_safe(OrchestratorTrigger, trigger_dict) if trigger_dict else OrchestratorTrigger()

    return HorizonContext(
        rcs=rcs,
        trigger=trigger,
        quarter=_from_dict_safe(QuarterFocus, profile.get("quarter") or {}),
        month=_from_dict_safe(MonthThemes, profile.get("month") or {}),
        week=_from_dict_safe(WeekHypothesis, profile.get("week") or {}),
        day=_from_dict_safe(DayEvents, profile.get("day") or {}),
        artifacts=_from_dict_safe(ArtifactsSummary, profile.get("artifacts") or {}),
        mastery_by_area=profile.get("mastery_by_area") or {},
        pilot_reflection=profile.get("pilot_reflection", ""),
        reflection_learned=profile.get("reflection_learned") or [],
        tomorrow_intention=profile.get("tomorrow_intention", ""),
    )


# ---------------------------------------------------------------------------
# decision_log + hard-fail gate (WP-483 Ф1, пир-сессия ход 3-4)
# ---------------------------------------------------------------------------

def build_decision_log(planner_result: dict, llm_ok: bool, timestamp: str) -> list[dict]:
    """Per-slot журнал происхождения: откуда взят каждый обязательный факт."""
    element_id = planner_result["plan_skeleton"]["element_id"]
    entries = [
        {
            "slot": "plan_day.element_choice",
            "source_file": "planner.py",
            "source_field": planner_result["decision_log"].get("element_choice", ""),
            "extraction_method": "derived" if element_id else "llm-assisted",
            "timestamp": timestamp,
        },
    ]
    if llm_ok:
        entries.append(
            {
                "slot": "narrative",
                "source_file": "llm_backend",
                "source_field": "narrative",
                "extraction_method": "llm-assisted",
                "timestamp": timestamp,
            }
        )
    return entries


def apply_hard_fail_gate(decision_log: list[dict], policy: dict) -> tuple[bool, str]:
    """Проверяет required_attribution_slots. Возвращает (passed, reason).

    confidence для extraction_method="llm-assisted" — константа из policy
    (задана автором policy заранее), не вычисляется здесь и не самооценивается
    LLM: иначе порог confidence проходит тривиально и hard-fail становится
    декорацией (найдено в пир-сессии, ход 3).
    """
    by_slot = {entry["slot"]: entry for entry in decision_log}
    for required in policy.get("required_attribution_slots", []):
        slot = required.get("slot")
        if not slot:
            return False, f"policy has a required_attribution_slots entry with no 'slot' key: {required!r}"
        accepted = set(required.get("accepted_methods", ["direct", "derived"]))
        entry = by_slot.get(slot)
        if entry is None:
            return False, f"required slot {slot!r} has no decision_log entry — adapter produced nothing for it"
        method = entry.get("extraction_method")
        if method not in accepted:
            return False, f"required slot {slot!r}: extraction_method={method!r} not in accepted {sorted(accepted)}"
        if method == "llm-assisted":
            confidence = required.get("llm_assisted_confidence")
            if confidence is None:
                return False, (
                    f"policy allows llm-assisted for slot {slot!r} but sets no "
                    f"llm_assisted_confidence — refusing to guess a value"
                )
            entry["confidence"] = confidence
    return True, ""


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

def render_markdown(narrative: str, plan_day: list[dict], decision_log: list[dict]) -> str:
    lines = ["# План на сегодня", "", narrative, "", "## Задания"]
    for item in plan_day:
        label = item.get("label") or item.get("element_id") or "?"
        tomatoes = item.get("tomatoes", 1)
        rationale = item.get("rationale", "")
        lines.append(f"- **{label}** ({tomatoes} помидорок) — {rationale}")
    lines += ["", "<!-- decision_log:", json.dumps(decision_log, ensure_ascii=False, indent=2), "-->"]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

@dataclass
class GuideResult:
    ok: bool
    markdown: str | None = None
    diagnostic: dict[str, Any] | None = None


def generate_daily_plan(
    profile_path: str,
    config_path: str | None = None,
    policy_path: str | None = None,
    seed: int | None = None,
) -> GuideResult:
    """profile.yaml → planner → LLM backend → hard-fail gate → markdown или диагностика.

    ok=True  → .markdown — готовый текст.
    ok=False → .diagnostic — причина отказа (не молчаливый пустой результат).
    """
    config = load_config(config_path)
    policy = load_policy(policy_path)
    backend_name = config.get("llm_backend", "anthropic")
    logger.info("generate_daily_plan: profile=%r backend=%r", profile_path, backend_name)

    curriculum_path = config.get("curriculum_path")
    if curriculum_path:
        os.environ["GUIDE_KIT_CURRICULUM_PATH"] = curriculum_path

    profile = load_profile(profile_path)
    ctx = build_horizon_context(profile)
    planner_result = plan_horizon(ctx, seed=seed)

    element_id = planner_result["plan_skeleton"]["element_id"]
    card_content = load_card_content(element_id, config.get("cards_path"))
    llm_input = dict(planner_result)
    if card_content:
        llm_input["card_content"] = card_content

    with open(DEFAULT_PROMPT_PATH, encoding="utf-8") as fh:
        system_prompt = fh.read()

    gen_context = GenerationContext(
        backend=backend_name,
        base_url=config.get("llm_base_url"),
        api_key=config.get("llm_api_key") or os.environ.get("GUIDE_KIT_LLM_API_KEY"),
        model=config.get("llm_model"),
    )
    llm_result = llm_generate(PromptSpec(system=system_prompt, user_json=llm_input), gen_context)

    timestamp = datetime.now(timezone.utc).isoformat()
    decision_log = build_decision_log(planner_result, llm_ok=llm_result.ok, timestamp=timestamp)

    if not llm_result.ok:
        logger.error("LLM backend %r failed: %s", backend_name, llm_result.error)
        return GuideResult(
            ok=False,
            diagnostic={
                "reason": f"LLM backend вызов не удался: {llm_result.error}",
                "backend": backend_name,
                "decision_log": decision_log,
                "timestamp": timestamp,
            },
        )

    passed, reason = apply_hard_fail_gate(decision_log, policy)
    if not passed:
        logger.error("hard-fail gate: %s", reason)
        return GuideResult(
            ok=False,
            diagnostic={"reason": reason, "decision_log": decision_log, "timestamp": timestamp},
        )

    try:
        llm_output = json.loads(llm_result.text)
    except json.JSONDecodeError as e:
        logger.error("LLM output is not valid JSON: %s", e)
        return GuideResult(
            ok=False,
            diagnostic={
                "reason": f"LLM вернул невалидный JSON: {e}",
                "raw_text_head": llm_result.text[:500],
                "decision_log": decision_log,
                "timestamp": timestamp,
            },
        )

    narrative = llm_output.get("narrative", "")
    plan_day = llm_output.get("plan_day", [])
    if not narrative or not plan_day:
        # decision_log только проверяет ПРОИСХОЖДЕНИЕ факта, не то, что LLM реально
        # что-то вернул — валидный JSON с пустым plan_day прошёл бы gate молча (ревью).
        logger.error("LLM returned valid JSON but empty content: narrative=%r plan_day=%r", bool(narrative), plan_day)
        return GuideResult(
            ok=False,
            diagnostic={
                "reason": "LLM вернул валидный JSON, но без содержимого (narrative и/или plan_day пусты) — руководство с пустым разделом не публикуется",
                "narrative_empty": not narrative,
                "plan_day_count": len(plan_day),
                "decision_log": decision_log,
                "timestamp": timestamp,
            },
        )

    markdown = render_markdown(narrative, plan_day, decision_log)
    return GuideResult(ok=True, markdown=markdown)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")

    parser = argparse.ArgumentParser(description="guide-kit generator adapter (WP-483 Ф1)")
    parser.add_argument("--profile", default="profile.yaml")
    parser.add_argument("--config", default="guide-kit.config.yaml")
    parser.add_argument("--policy", default=None)
    parser.add_argument("--out", default=None, help="куда писать результат; по умолчанию — stdout")
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args()

    result = generate_daily_plan(args.profile, args.config, args.policy, seed=args.seed)
    if result.ok:
        output = result.markdown
    else:
        # диагностический YAML — не молчаливый пустой результат (hard-fail policy)
        output = "---\n" + yaml.safe_dump(result.diagnostic, allow_unicode=True, sort_keys=False) + "---\n"

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(output)
    else:
        print(output)

    sys.exit(0 if result.ok else 1)
