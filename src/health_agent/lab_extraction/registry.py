"""Extensible names and exact unit equivalences; never infer a conversion factor."""

import hashlib
import re
from decimal import Decimal, InvalidOperation

# Each entry is canonical name, source aliases, allowed unit families. Distinct
# physical units remain distinct: mg/dL is NOT guessed to equal mmol/L.
_ANALYTES = (
    ("ferritin", "Ferritin|Ферритин", "ng/mL|ug/L"),
    ("vitamin_b12", "B12|Vitamin B12|Витамин B12|Кобаламин", "pg/mL|pmol/L"),
    (
        "folate",
        "Folate|Folic acid|Фолат|Фолиевая кислота|Vitamin B9|Витамин B9|B9",
        "ng/mL|nmol/L",
    ),
    ("vitamin_d", "Vitamin D|Витамин D|25-OH Vitamin D|25(OH)D", "ng/mL|nmol/L"),
    (
        "total_cholesterol",
        "Total cholesterol|Cholesterol|Холестерин общий|Общий холестерин",
        "mmol/L|mg/dL",
    ),
    ("ldl_cholesterol", "LDL|LDL cholesterol|ЛПНП|Холестерин ЛПНП", "mmol/L|mg/dL"),
    ("hdl_cholesterol", "HDL|HDL cholesterol|ЛПВП|Холестерин ЛПВП", "mmol/L|mg/dL"),
    ("triglycerides", "Triglycerides|Триглицериды", "mmol/L|mg/dL"),
    ("iron", "Iron|Железо|Сывороточное железо", "umol/L|ug/dL"),
    ("transferrin", "Transferrin|Трансферрин", "g/L|mg/dL"),
    ("transferrin_saturation", "Transferrin saturation|Насыщение трансферрина", "%"),
    ("hemoglobin", "Hemoglobin|Haemoglobin|HGB|Hb|Гемоглобин", "g/L|g/dL"),
    ("hematocrit", "Hematocrit|HCT|Гематокрит", "%|L/L"),
    ("red_blood_cells", "RBC|Red blood cells|Эритроциты", "10^12/L"),
    ("white_blood_cells", "WBC|White blood cells|Лейкоциты", "10^9/L"),
    ("platelets", "Platelets|PLT|Тромбоциты", "10^9/L"),
    (
        "mcv",
        (
            "MCV|Средний объем эритроцита|Средний объём эритроцита|"
            "Средний объем эритроцитов (MCV)"
        ),
        "fL",
    ),
    (
        "mch",
        (
            "MCH|Среднее содержание гемоглобина|"
            "Среднее содержание гемоглобина в эритроците (МСН)"
        ),
        "pg",
    ),
    (
        "mchc",
        (
            "MCHC|Средняя концентрация гемоглобина|"
            "Средняя концентрация Hb в эритроцитах (МСНС)"
        ),
        "g/L|g/dL",
    ),
    ("rdw", "RDW|RDW-CV", "%"),
    (
        "mpv",
        "MPV|Средний объем тромбоцита|Средний объем тромбоцитов (MPV)",
        "fL",
    ),
    ("neutrophils", "Neutrophils|Нейтрофилы|NEUT|NEUT#|NEUT%", "%|10^9/L"),
    ("lymphocytes", "Lymphocytes|Лимфоциты|LYMPH|LYMPH#|LYMPH%", "%|10^9/L"),
    ("monocytes", "Monocytes|Моноциты|MONO|MONO#|MONO%", "%|10^9/L"),
    ("eosinophils", "Eosinophils|Эозинофилы|EOS|EOS#|EOS%", "%|10^9/L"),
    ("basophils", "Basophils|Базофилы|BASO|BASO#|BASO%", "%|10^9/L"),
    ("esr", "ESR|СОЭ|Скорость оседания эритроцитов", "mm/h"),
    ("glucose", "Glucose|Глюкоза", "mmol/L|mg/dL"),
    ("hba1c", "HbA1c|Гликированный гемоглобин", "%|mmol/mol"),
    ("creatinine", "Creatinine|Креатинин", "umol/L|mg/dL"),
    ("urea", "Urea|Мочевина", "mmol/L|mg/dL"),
    ("uric_acid", "Uric acid|Мочевая кислота", "umol/L|mg/dL"),
    ("egfr", "eGFR|СКФ|Скорость клубочковой фильтрации", "mL/min/1.73m2"),
    ("alt", "ALT|АЛТ|Аланинаминотрансфераза", "U/L"),
    ("ast", "AST|АСТ|Аспартатаминотрансфераза", "U/L"),
    ("ggt", "GGT|ГГТ|ГГТП|Гамма-глутамилтрансфераза", "U/L"),
    ("alkaline_phosphatase", "ALP|Alkaline phosphatase|Щелочная фосфатаза", "U/L"),
    ("ldh", "LDH|ЛДГ|Лактатдегидрогеназа", "U/L"),
    (
        "total_bilirubin",
        "Total bilirubin|Билирубин общий|Общий билирубин",
        "umol/L|mg/dL",
    ),
    (
        "direct_bilirubin",
        "Direct bilirubin|Билирубин прямой|Прямой билирубин",
        "umol/L|mg/dL",
    ),
    ("albumin", "Albumin|Альбумин", "g/L|g/dL"),
    ("total_protein", "Total protein|Общий белок|Белок общий", "g/L|g/dL"),
    ("sodium", "Sodium|Натрий|Na", "mmol/L"),
    ("potassium", "Potassium|Калий|K", "mmol/L"),
    ("chloride", "Chloride|Хлориды|Хлор", "mmol/L"),
    ("calcium", "Calcium|Кальций|Кальций общий", "mmol/L|mg/dL"),
    ("magnesium", "Magnesium|Магний", "mmol/L|mg/dL"),
    ("phosphate", "Phosphate|Phosphorus|Фосфор|Фосфаты", "mmol/L|mg/dL"),
    ("crp", "CRP|C-reactive protein|С-реактивный белок|СРБ", "mg/L|mg/dL"),
    ("tsh", "TSH|ТТГ|Тиреотропный гормон", "mIU/L|uIU/mL"),
    ("free_t4", "Free T4|FT4|Т4 свободный|Тироксин свободный", "pmol/L|ng/dL"),
    ("free_t3", "Free T3|FT3|Т3 свободный", "pmol/L|pg/mL"),
    (
        "testosterone",
        "Testosterone|Тестостерон|Тестостерон общий",
        "nmol/L|ng/mL|ng/dL",
    ),
    ("estradiol", "Estradiol|Эстрадиол", "pmol/L|pg/mL"),
    ("prolactin", "Prolactin|Пролактин", "ng/mL|mIU/L"),
    ("progesterone", "Progesterone|Прогестерон", "nmol/L|ng/mL"),
    ("cortisol", "Cortisol|Кортизол", "nmol/L|ug/dL"),
    ("insulin", "Insulin|Инсулин", "uIU/mL|pmol/L"),
    ("psa", "PSA|ПСА|Простатический специфический антиген", "ng/mL"),
)

_UNIT_ALIASES = {
    "ng/mL": "нг/мл",
    "ug/L": "µg/l|μg/l|мкг/л",
    "pg/mL": "пг/мл",
    "pmol/L": "пмоль/л",
    "nmol/L": "нмоль/л",
    "mmol/L": "ммоль/л",
    "umol/L": "µmol/l|μmol/l|мкмоль/л",
    "mg/dL": "мг/дл",
    "ug/dL": "µg/dl|мкг/дл",
    "g/L": "г/л",
    "g/dL": "г/дл",
    "mg/L": "мг/л",
    "U/L": "ед/л|u/l|iu/l|ме/л",
    "mIU/L": "мме/л",
    "uIU/mL": "µiu/ml|μiu/ml|мкме/мл",
    "10^9/L": "10^9/л|10*9/л|10⁹/л|10⁹/l|10*9/l",
    "10^12/L": "10^12/л|10*12/л|10¹²/л|10¹²/l|10*12/l",
    "fL": "фл",
    "pg": "пг|пг/кл",
    "mm/h": "мм/ч",
    "%": "%",
}


def name_key(value: str) -> str:
    return " ".join(value.casefold().replace("ё", "е").split())


def unit_key(value: str) -> str:
    return value.strip().casefold().replace("μ", "µ")


_NAMES = {
    name_key(alias): name
    for name, aliases, _ in _ANALYTES
    for alias in (name, *aliases.split("|"))
}
_UNITS = {
    unit_key(alias): canonical
    for _, _, units in _ANALYTES
    for canonical in units.split("|")
    for alias in (canonical, *_UNIT_ALIASES.get(canonical, "").split("|"))
    if alias
}
_ALLOWED = {name: frozenset(units.split("|")) for name, _, units in _ANALYTES}


def canonical_name(source_name: str) -> str:
    key = name_key(source_name)
    return _NAMES.get(key, "unmapped_" + hashlib.sha256(key.encode()).hexdigest()[:20])


def known_unit(source_unit: str) -> bool:
    return unit_key(source_unit) in _UNITS


def bounded_decimal(raw: str) -> Decimal:
    if (
        len(raw) > 64
        or re.fullmatch(
            r"[+-]?(?:[0-9]+(?:[.,][0-9]+)?|[.,][0-9]+)(?:[eE][+-]?[0-9]+)?", raw
        )
        is None
    ):
        raise ValueError("invalid_lab_decimal")
    try:
        result = Decimal(raw.replace(",", "."))
    except InvalidOperation:
        raise ValueError("invalid_lab_decimal") from None
    exponent = result.as_tuple().exponent
    if (
        not result.is_finite()
        or not isinstance(exponent, int)
        or not -12 <= exponent <= 12
        or result.copy_abs() > Decimal("1e12")
    ):
        raise ValueError("invalid_lab_decimal")
    return result


def normalize_registered(name: str, raw: str, unit: str) -> tuple[Decimal, str]:
    canonical_unit = _UNITS.get(unit_key(unit))
    if canonical_unit is None or canonical_unit not in _ALLOWED.get(name, ()):
        raise ValueError("unsupported_lab_normalization")
    return bounded_decimal(raw), canonical_unit
