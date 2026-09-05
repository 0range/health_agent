"""Small versioned educational catalogue, separate from patient evidence."""

from __future__ import annotations

from dataclasses import dataclass

CATALOG_VERSION = "2026-09-05.1"


@dataclass(frozen=True, slots=True)
class Explanation:
    key: str
    general_knowledge: str
    source_url: str
    possible_next_step: str


# No analyte-specific target or interpretation rule belongs here without review.
EXPLANATIONS: dict[str, Explanation] = {
    "laboratory_reference": Explanation(
        key="laboratory_reference",
        general_knowledge=(
            "Референсные диапазоны зависят от лаборатории и метода. Результат внутри "
            "диапазона сам по себе не доказывает здоровье, а результат вне диапазона "
            "сам по себе не является диагнозом."
        ),
        source_url=(
            "https://medlineplus.gov/lab-tests/how-to-understand-your-lab-results/"
        ),
        possible_next_step=(
            "Обсудить результат с врачом вместе с симптомами, анамнезом и диапазоном "
            "именно этой лаборатории."
        ),
    ),
    "whoop_sleep": Explanation(
        key="whoop_sleep",
        general_knowledge="WHOOP предоставляет поля продолжительности и стадий сна.",
        source_url="https://developer.whoop.com/docs/developing/user-data/sleep/",
        possible_next_step="Проверить полноту синхронизации и наблюдать свою динамику.",
    ),
    "whoop_recovery": Explanation(
        key="whoop_recovery",
        general_knowledge="WHOOP предоставляет показатель восстановления и его компоненты.",
        source_url="https://developer.whoop.com/docs/developing/user-data/recovery/",
        possible_next_step="Использовать изменение как наблюдение, а не диагноз.",
    ),
}


def explain(key: str | None) -> Explanation | None:
    """Return reviewed education or ``None``; callers must not fabricate it."""

    return EXPLANATIONS.get(key) if key else None


GENERIC_EXPLANATION_RU = (
    "Для этого показателя пока нет проверенной справочной статьи. "
    "Оценивать результат следует по диапазону лаборатории и вместе с врачом."
)
