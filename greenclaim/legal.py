"""Логическая матрица квалификации нарушений.

Ответы пользователя -> теги -> правила (data/legal_matrix.json) ->
нормы права, ведомства-адресаты и просительная часть жалобы.
Никакого ML: результат полностью детерминирован и проверяем юристом.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .config import DATA_DIR


@dataclass(frozen=True)
class Norm:
    id: str
    act: str
    article: str
    title: str
    confidence: str = "high"

    def render(self) -> str:
        head = f"{self.act}, {self.article}" if self.article else self.act
        return f"{head} — {self.title}" if self.title else head


@dataclass
class Qualification:
    scenario: str
    violation_label: str
    norms: list[Norm] = field(default_factory=list)
    agency_ids: list[str] = field(default_factory=list)
    requests: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    tags: set[str] = field(default_factory=set)
    applied_rules: list[str] = field(default_factory=list)

    @property
    def needs_legal_check(self) -> bool:
        return any(n.confidence != "high" for n in self.norms)


class LegalMatrix:
    def __init__(self, path: Path | None = None):
        raw = json.loads((path or DATA_DIR / "legal_matrix.json").read_text(encoding="utf-8"))
        self.norms = {k: Norm(id=k, **v) for k, v in raw["norms"].items()}
        self.scenarios: dict = raw["scenarios"]
        self.rules: list[dict] = raw["rules"]

    def questions(self, scenario: str) -> list[dict]:
        return self.scenarios[scenario]["questions"]

    def _tags_for(self, scenario: str, answers: dict[str, str]) -> set[str]:
        tags: set[str] = set()
        for q in self.questions(scenario):
            val = answers.get(q["id"])
            for opt in q["options"]:
                if opt["value"] == val:
                    tags.update(opt["tags"])
        return tags

    @staticmethod
    def _matches(rule: dict, tags: set[str]) -> bool:
        if_all, if_any = rule.get("if_all", []), rule.get("if_any", [])
        if not if_all and not if_any:
            return True
        return all(t in tags for t in if_all) and (not if_any or any(t in tags for t in if_any))

    def qualify(self, scenario: str, answers: dict[str, str]) -> Qualification:
        if scenario not in self.scenarios:
            raise KeyError(f"Неизвестный сценарий: {scenario}")
        tags = self._tags_for(scenario, answers)
        q = Qualification(
            scenario=scenario,
            violation_label=self.scenarios[scenario]["violation_label"],
            tags=tags,
        )
        for rule in self.rules:
            if rule["scenario"] != scenario or not self._matches(rule, tags):
                continue
            q.applied_rules.append(rule["name"])
            for nid in rule.get("norms", []):
                n = self.norms[nid]
                if n not in q.norms:
                    q.norms.append(n)
            for aid in rule.get("agencies", []):
                if aid not in q.agency_ids:
                    q.agency_ids.append(aid)
            for r in rule.get("requests", []):
                if r not in q.requests:
                    q.requests.append(r)
            for f in rule.get("flags", []):
                if f not in q.flags:
                    q.flags.append(f)
        # профильное ведомство — основной адресат, прокуратура — всегда последняя
        if "prosecutor" in q.agency_ids:
            q.agency_ids.remove("prosecutor")
            q.agency_ids.append("prosecutor")
        return q
