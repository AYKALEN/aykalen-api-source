"""Full calculation for the mobile app: natal + current annual cycle (engine 0.5.2, D1 only).

Runs inside a separate worker process (see main.py) so a multi-second calculation never blocks
the site's /first-map. This module imports only the vendored engine, never pandas or main.py.
Birth data is never logged or stored; exceptions carry no input values.
"""
from __future__ import annotations

import sys
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytz

_VENDOR = str(Path(__file__).resolve().parent / "vendor")
if _VENDOR not in sys.path:
    sys.path.insert(0, _VENDOR)

from astro_engine import __version__ as ENGINE_VERSION  # noqa: E402
from astro_engine.api import calculate as engine_calculate  # noqa: E402

CONTRACT = "aykalen-calculation-1"
# The engine's house routine is only validated inside the polar circles.
MAX_ABS_LATITUDE = 66.0

SIGNS = dict(zip(
    ["Baran", "Byk", "Bliźnięta", "Rak", "Lew", "Panna", "Waga", "Skorpion", "Strzelec", "Koziorożec", "Wodnik", "Ryby"],
    ["Aries", "Taurus", "Gemini", "Cancer", "Leo", "Virgo", "Libra", "Scorpio", "Sagittarius", "Capricorn", "Aquarius", "Pisces"],
))


class CalculationUnavailable(Exception):
    """The engine cannot produce a cycle for this input (no input values in the message)."""


# --- Wire projection: identical to aykalen-mobile/computation_service/projection.py ---

def _utc(jd):
    instant = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(days=jd - 2440587.5)
    return instant.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _interval(start, end):
    return {"start_jd_ut": start, "end_jd_ut": end, "start_utc": _utc(start), "end_utc": _utc(end)}


def _period(row, children=False):
    result = {"lord": row["lord"],
              "window": _interval(row["start_jd"], row["end_jd"]),
              "source": _interval(row["full_start_jd"], row["full_end_jd"])}
    if children:
        result["antardashas"] = [_period(ad) for ad in row["antardashas"]]
    return result


def _chart(data):
    source = data["charts"]["D1"]
    return {
        "jd_ut": data["jd_ut"], "ayanamsa_degrees": data["ayanamsa_degrees"],
        "ephemeris": deepcopy(data["ephemeris"]),
        "positions": {code: {
            "sign": SIGNS[p["zodiac_sign"]], "house": p["house"],
            "longitude_degrees": p["position_degrees_exact"],
            "rules_houses": p["rules_houses"], "dispositor": p["dispositor"],
        } for code, p in source["positions"].items()},
        "house_lords": [{"house": h["house"], "sign": SIGNS[h["sign"]],
                         "lord": h["lord"], "lord_house": h["lord_house"],
                         "lord_sign": SIGNS[h["lord_sign"]]} for h in source["house_lords"]],
    }


def project(raw):
    if raw["schema_version"] != "astro-data-1" or raw["engine_version"] != "0.5.2":
        raise ValueError("Unreviewed engine version")
    if raw["scope"]["exported_vargas"] != ["D1"]:
        raise ValueError("Expected D1-only profile")
    annual = raw["annual"]
    schedule = annual["tithi_ashtottari"]
    relation = annual["lagna_distances"]["charts"]["D1"]
    return {
        "schema_version": CONTRACT,
        "metadata": {"engine_version": raw["engine_version"], "engine_schema_version": raw["schema_version"],
                     "software": deepcopy(raw["software"]), "calculation_profile": deepcopy(raw["calculation_profile"]),
                     "engine_scope": deepcopy(raw["scope"]), "limitations": list(raw["limitations"]), "projection": "d1-annual-core-1"},
        "natal": _chart(raw["natal"]),
        "annual": {"solar_ingress_year": annual["target_year"],
                   "window": _interval(schedule["tp_start_jd"], schedule["tp_end_jd"]),
                   "chart": _chart(annual), "phase_residual_degrees": annual["phase_residual_degrees"],
                   "lagna_relation": {"direction": "natal_to_tp", "from_sign": SIGNS[relation["from"]],
                                      "to_sign": SIGNS[relation["to"]], "inclusive_sign_count": relation["inclusive_sign_count"]},
                   "tithi_ashtottari": {"profile_version": schedule["profile_version"], "compression_basis": schedule["compression_basis"],
                                         "year_tithis": schedule["tp_year_tithis"], "periods": [_period(p, True) for p in schedule["periods"]]}},
    }


# --- Civil time: the same instant the site (/first-map) uses ---

def engine_local_time(date: str, time: str, tz_name: str) -> tuple[str, str, str]:
    """Return (date, time, timezone) the engine accepts for the instant the site computes.

    The site localizes with pytz's default (is_dst=False) and never rejects a time. The engine
    rejects nonexistent and ambiguous local times, so:
    - nonexistent (spring-forward gap): pass the equivalent valid local time (same instant);
    - ambiguous (fall-back hour): pass the same instant in a fixed-offset zone.
    """
    tz = pytz.timezone(tz_name)
    naive = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M:%S")
    try:
        tz.localize(naive, is_dst=None)
        return date, time, tz_name
    except pytz.NonExistentTimeError:
        valid = tz.normalize(tz.localize(naive, is_dst=False))
        return valid.strftime("%Y-%m-%d"), valid.strftime("%H:%M:%S"), tz_name
    except pytz.AmbiguousTimeError:
        offset = tz.localize(naive, is_dst=False).utcoffset()
        minutes = int(offset.total_seconds() // 60)
        if minutes % 60:
            raise CalculationUnavailable("ambiguous local time in a zone with a fractional offset")
        hours = minutes // 60
        # POSIX-style names invert the sign: UTC+1 is Etc/GMT-1.
        fixed = "Etc/GMT" if hours == 0 else f"Etc/GMT{'-' if hours > 0 else '+'}{abs(hours)}"
        return date, time, fixed


# --- Cycle selection ---

def _engine_request(date: str, time: str, tz_name: str, lat: float, lng: float, year: int) -> dict:
    return {"birth": {"date": date, "time": time, "timezone": tz_name, "latitude": lat, "longitude": lng},
            "target_year": year, "sunrise_mode": "center", "allowed_vargas": ["D1"], "techniques": []}


def _calculate_year(date, time, tz_name, lat, lng, year):
    try:
        return project(engine_calculate(_engine_request(date, time, tz_name, lat, lng, year)))
    except (ValueError, KeyError) as exc:
        raise CalculationUnavailable(type(exc).__name__) from None


def compute(date: str, time: str, tz_name: str, lat: float, lng: float, now_utc: datetime | None = None) -> dict:
    """Natal chart and the annual cycle that contains now (start-inclusive, end-exclusive).

    The calendar year is only a first guess. A cycle that starts after now means the previous
    year; one that ended means the next. At most two engine runs.
    """
    if abs(lat) > MAX_ABS_LATITUDE:
        raise CalculationUnavailable("latitude outside the supported range")
    now = now_utc or datetime.now(timezone.utc)
    at = now.timestamp() / 86400 + 2440587.5
    date, time, tz_name = engine_local_time(date, time, tz_name)
    birth_year = int(date[:4])
    year = max(now.year, birth_year)
    tried = set()
    for _ in range(3):
        if year in tried or year < birth_year:
            break
        tried.add(year)
        result = _calculate_year(date, time, tz_name, lat, lng, year)
        window = result["annual"]["window"]
        if window["start_jd_ut"] <= at < window["end_jd_ut"]:
            return result
        year = year - 1 if at < window["start_jd_ut"] else year + 1
    raise CalculationUnavailable("no annual cycle contains the current date")
