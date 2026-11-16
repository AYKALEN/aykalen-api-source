"""
Whole-sign house of the ascendant lord from (Ascendant sign, ruler's sign).

Matches engine.ChartData: house = (ruler_zodiac_index - asc_zodiac_index) % 12 + 1.
Used for CSV columns and LLM naming; API returns the same value from chart_data for the user.
"""

from __future__ import annotations

# Same order as engine.ZODIAC_SIGNS / English labels in API
ZODIAC_EN: tuple[str, ...] = (
    "Aries",
    "Taurus",
    "Gemini",
    "Cancer",
    "Leo",
    "Virgo",
    "Libra",
    "Scorpio",
    "Sagittarius",
    "Capricorn",
    "Aquarius",
    "Pisces",
)

# Short English theme for archetype naming / prompts (plain language)
RULER_HOUSE_THEMES: dict[int, str] = {
    1: "self_identity",
    2: "values_resources",
    3: "communication",
    4: "home_roots",
    5: "creativity_joy",
    6: "daily_work_health",
    7: "partnership",
    8: "depth_change",
    9: "beliefs_growth",
    10: "career_public",
    11: "community_groups",
    12: "inner_life",
}


def ruler_house_whole_sign(ascendant_en: str, ruler_sign_en: str) -> int:
    """1–12 whole-sign house of the asc lord from ascendant and lord's sign."""
    try:
        ai = ZODIAC_EN.index(ascendant_en)
        ri = ZODIAC_EN.index(ruler_sign_en)
    except ValueError as e:
        raise ValueError(f"Unknown sign: asc={ascendant_en!r}, ruler={ruler_sign_en!r}") from e
    return (ri - ai) % 12 + 1


def ruler_house_theme(house: int) -> str:
    """Stable slug for naming pipelines; falls back for invalid input."""
    return RULER_HOUSE_THEMES.get(int(house), "unknown")
