from decimal import Decimal
from hashlib import sha256

import pytest

from health_agent.lab_extraction.registry import (
    canonical_name,
    known_unit,
    normalize_registered,
)


@pytest.mark.parametrize(
    "name,unit",
    [
        ("indirect_bilirubin", "umol/L"),
        ("indirect_bilirubin", "mg/dL"),
        ("thrombocrit", "%"),
        ("myelocytes", "%"),
        ("metamyelocytes", "%"),
        ("band_neutrophils", "%"),
        ("segmented_neutrophils", "%"),
        ("reticulocytes", "%"),
        ("immature_reticulocyte_fraction", "%"),
        ("low_fluorescence_reticulocyte_fraction", "%"),
        ("medium_fluorescence_reticulocyte_fraction", "%"),
        ("high_fluorescence_reticulocyte_fraction", "%"),
        ("monomeric_prolactin_recovery", "%"),
        ("salivary_free_testosterone", "ng/mL"),
        ("salivary_cortisone", "ng/mL"),
        ("salivary_17oh_progesterone", "ng/mL"),
        ("salivary_free_progesterone", "ng/mL"),
        ("salivary_androstenedione", "ng/mL"),
        ("salivary_dehydroepiandrosterone", "ng/mL"),
        ("salivary_free_estradiol", "pg/mL"),
        ("urine_squamous_epithelial_cells", "cells/uL"),
        ("urine_white_blood_cells", "cells/uL"),
        ("urine_red_blood_cells", "cells/uL"),
        ("insulin", "uU/mL"),
    ],
)
def test_reviewed_canonical_identity_preserves_exact_unit(name, unit):
    assert canonical_name(name) == name
    assert normalize_registered(name, "1.25", unit) == (Decimal("1.25"), unit)


@pytest.mark.parametrize(
    "source",
    [
        "Билирубин непрямой",
        "Тромбокрит",
        "Миелоциты",
        "Метамиелоциты",
        "Ретикулоциты",
        "Тестостерон свободный (слюна)",
        "Кортизон (слюна)",
        "Эстрадиол свободный (слюна)",
        "Эпителий плоский",
        "Пролактин мономерный (пост ПЭГ), %",
        "",
    ],
)
def test_source_labels_still_unmapped(source):
    key = " ".join(source.casefold().replace("ё", "е").split())
    assert canonical_name(source) == "unmapped_" + sha256(key.encode()).hexdigest()[:20]
    with pytest.raises(ValueError, match="unsupported_lab_normalization"):
        normalize_registered(canonical_name(source), "1.25", "ng/mL")


@pytest.mark.parametrize(
    "source,name",
    [
        ("Лейкоциты", "white_blood_cells"),
        ("Эритроциты", "red_blood_cells"),
    ],
)
def test_generic_source_blood_names_do_not_become_urine(source, name):
    assert canonical_name(source) == name
    with pytest.raises(ValueError, match="unsupported_lab_normalization"):
        normalize_registered(name, "1.25", "cells/uL")


@pytest.mark.parametrize("unit", ["мкЕд/мл", "кл/мкл", "клеток/мкл"])
def test_source_unit_spellings_stay_unknown(unit):
    assert not known_unit(unit)


def test_insulin_international_units_stay_separate():
    assert normalize_registered("insulin", "1.25", "мкМЕ/мл") == (
        Decimal("1.25"),
        "uIU/mL",
    )
    assert normalize_registered("insulin", "1.25", "uU/mL") == (
        Decimal("1.25"),
        "uU/mL",
    )


@pytest.mark.parametrize(
    "name,unit",
    [
        ("amylase", "U/L"),
        ("pdw", "fL"),
        ("pdw", "%"),
        ("rdw_sd", "fL"),
        ("macrocytes", "%"),
        ("microcytes", "%"),
        ("immature_granulocytes", "%"),
        ("platelet_large_cell_ratio", "%"),
        ("reticulocytes_absolute", "10^9/L"),
        ("salivary_cortisol", "ng/mL"),
        ("fsh", "mIU/mL"),
        ("lh", "mIU/mL"),
        ("shbg", "nmol/L"),
        ("creatine_kinase", "U/L"),
        ("vldl_cholesterol", "mmol/L"),
        ("non_hdl_cholesterol", "mmol/L"),
        ("dhea_sulfate", "ug/dL"),
        ("anti_tpo", "IU/mL"),
        ("atherogenic_index", "1"),
        ("urine_ph", "1"),
        ("urine_specific_gravity", "1"),
    ],
)
def test_completed_canonical_identity_preserves_exact_unit(name, unit):
    assert canonical_name(name) == name
    assert normalize_registered(name, "1.25", unit) == (Decimal("1.25"), unit)


@pytest.mark.parametrize(
    "source", ["Амилаза", "Кортизол в слюне", "Индекс атерогенности"]
)
def test_completed_source_labels_still_unmapped(source):
    assert canonical_name(source).startswith("unmapped_")
