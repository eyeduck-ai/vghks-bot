"""Conservative display groups; classification never filters clinical orders."""
from .analysis_numeric import MODULES, term_match

COMMON_OPHTHALMIC_ORDERS = tuple(dict.fromkeys(
    term for module in ("retina", "cataract") for term in MODULES[module]["orders"]))
COMMON_ALIASES = ("OCT", "Optical coherence tomography", "光學同調斷層",
                  "Visual Field", "Perimetry", "視野", "眼底攝影")


def display_group(name):
    fundus = term_match(name, "Fundus")
    non_ocular = fundus and (any(term_match(name, word) for word in ("gastric", "stomach")) or "胃" in name)
    photographed_fundus = fundus and any(term_match(name, word) for word in (
        "photo", "photograph", "photography", "image", "images", "imaging"))
    common = not non_ocular and (photographed_fundus or
        any(term_match(name, term) for term in (*COMMON_OPHTHALMIC_ORDERS, *COMMON_ALIASES)))
    return {"ophthalmic": common,
            "display_group": "常用眼科檢查" if common else "其他醫囑"}
