"""Per-language glossaries for Nigerian market bookkeeping."""

from typing import Any

# Standard market measures and units across Nigerian markets
COMMON_UNITS = [
    "bag", "carton", "sachet", "derica", "congo", "paint bucket", "paint",
    "keg", "mudub", "rubber", "piece", "tian", "crate", "half-bag", "quarter-bag",
]

GLOSSARIES: dict[str, dict[str, list[str]]] = {
    "pcm": {
        "items": [
            "rice", "beans", "garri", "indomie", "milo", "sugar", "groundnut oil",
            "palm oil", "spaghetti", "peak milk", "dano", "maggi", "salt", "semovita",
            "wheat", "cement", "pure water", "soft drink", "generator fuel",
        ],
        "units": COMMON_UNITS,
        "customers": [
            "Mama Ngozi", "Papa Segun", "Mama Chika", "Bro Emeka", "Sister Blessing",
            "Alhaji Musa", "Iya Moria", "Baba Aisha", "Alhaja",
        ],
    },
    "en": {
        "items": [
            "rice", "beans", "cassava flakes", "noodles", "milo", "sugar", "vegetable oil",
            "palm oil", "spaghetti", "milk", "seasoning cubes", "salt", "flour",
            "cement", "sachet water", "soft drinks", "fuel",
        ],
        "units": COMMON_UNITS,
        "customers": [
            "Mr Adeleke", "Mrs Okonkwo", "Alhaji Musa", "Chief Okafor", "Pastor David",
            "Madam Grace", "Brother John",
        ],
    },
    "yo": {
        "items": [
            "iresi", "ewa", "garri", "indomie", "milo", "suga", "epo pupa", "ororo",
            "ata", "alubosa", "iyo", "elubo", "semovita", "epo petirolu", "omitoro",
        ],
        "units": [
            "apo", "katoni", "saseti", "kongo", "garawa", "kegi", "piti", "ege",
        ] + COMMON_UNITS,
        "customers": [
            "Iya Ngozi", "Baba Tunde", "Alhaja Kafaya", "Iya Segun", "Baba Ibeji",
            "Alhaji Musa", "Mama Moria", "Baba Aladiye",
        ],
    },
    "ha": {
        "items": [
            "shinkafa", "wake", "garin kwaki", "taliya", "milo", "sukari", "manja",
            "mangyada", "magi", "gishiri", "fulawa", "masara", "mai fetur",
        ],
        "units": [
            "buhu", "kwali", "muduk", "tiya", "roba", "kofi", "keke",
        ] + COMMON_UNITS,
        "customers": [
            "Malam Musa", "Alhaji Bello", "Hajiya Fatima", "Mallam Garba", "Audu",
            "Shehu", "Aisha", "Kabiru",
        ],
    },
    "ig": {
        "items": [
            "osikapa", "agwa", "garri", "indomie", "milo", "shuga", "mmanu nri",
            "mmanu akwu", "nnu", "azu", "anwuru", "petrol", "ncha",
        ],
        "units": [
            "akpa", "katon", "derica", "kongo", "boketi", "keg", "ibe",
        ] + COMMON_UNITS,
        "customers": [
            "Mama Ngozi", "Oga Chika", "Nna Emeka", "Adaeze", "Dede Okonkwo",
            "Mazi Obi", "Nwanyi", "Onyeze",
        ],
    },
}


def get_glossary(lang: str) -> dict[str, list[str]]:
    """Retrieve glossary for specified language, defaulting to Pidgin."""
    return GLOSSARIES.get(lang, GLOSSARIES["pcm"])


def get_glossary_keywords(lang: str) -> list[str]:
    """Flattens glossary into a single list of keywords for ASR prompting."""
    g = get_glossary(lang)
    all_kws = []
    for words in g.values():
        all_kws.extend(words)
    return list(dict.fromkeys(all_kws))
