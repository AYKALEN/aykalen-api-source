import asyncio
import logging
import multiprocessing
import os
import threading
import time
from collections import deque
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from datetime import date, datetime

import pandas as pd
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from timezonefinder import TimezoneFinder

import app_calculation

from archetype_house import ruler_house_theme as archetype_ruler_house_theme
from engine import ChartData
from first_map import (
    UNKNOWN_SCAN_STEP_MINUTES,
    iter_times_in_window,
    resolve_birth_time,
    resolve_time_window,
    SCAN_STEP_MINUTES,
)

logger = logging.getLogger("aykalen.api")

# The docs pages (/docs, /redoc, /openapi.json) are public when on; nothing on the site uses
# them, so production keeps them off. API_DOCS=1 turns them on for local work.
_docs_on = os.getenv("API_DOCS") == "1"
app = FastAPI(
    title="AYKALEN API",
    version="1.0.0",
    docs_url="/docs" if _docs_on else None,
    redoc_url="/redoc" if _docs_on else None,
    openapi_url="/openapi.json" if _docs_on else None,
)

# innercodex.app and aykalen.app only redirect to aykalen.com, so only aykalen.com calls the API.
_default_origins = "https://aykalen.com,https://www.aykalen.com"
_allowed_origins = [
    o.strip() for o in os.getenv("CORS_ALLOWED_ORIGINS", _default_origins).split(",") if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
    allow_credentials=False,
)

# Shown to the visitor as-is (the frontend renders `detail`). Details go to the server log only,
# and never the birth data itself.
MAP_ERROR = "Could not compute the map for these details."

ZODIAC_PL_TO_EN = {
    "Baran": "Aries",
    "Byk": "Taurus",
    "Bliźnięta": "Gemini",
    "Rak": "Cancer",
    "Lew": "Leo",
    "Panna": "Virgo",
    "Waga": "Libra",
    "Skorpion": "Scorpio",
    "Strzelec": "Sagittarius",
    "Koziorożec": "Capricorn",
    "Wodnik": "Aquarius",
    "Ryby": "Pisces",
}

ARCHETYPES_CSV = os.path.join(os.path.dirname(__file__), "data", "VS_innercodex_archetypes.csv")

try:
    archetypes_df = pd.read_csv(ARCHETYPES_CSV)
except FileNotFoundError:
    raise RuntimeError(f"Archetypes file not found: {ARCHETYPES_CSV}")


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError):
    # The default body echoes the submitted values back; log only which fields failed.
    fields = sorted({".".join(str(p) for p in err.get("loc", ())[1:]) for err in exc.errors()})
    logger.info("request validation failed: fields=%s", fields)
    if request.url.path.startswith("/v1/app/"):
        return _app_error(422, APP_ERROR)
    return JSONResponse(status_code=422, content={"detail": MAP_ERROR})


def _map_error(status_code: int, reason: str) -> HTTPException:
    logger.error("first-map failed: %s", reason)
    return HTTPException(status_code=status_code, detail=MAP_ERROR)


def _check_time(value: str | None, *, allow_hhmm: bool) -> str | None:
    """HH:MM:SS (or HH:MM when allowed), two digits each, a real clock time."""
    if value is None:
        return None
    fmt = "%H:%M" if allow_hhmm and len(value) == 5 else "%H:%M:%S"
    if len(value) != len("00:00:00" if fmt == "%H:%M:%S" else "00:00"):
        raise ValueError("time must be HH:MM" + (" or HH:MM:SS" if allow_hhmm else ":SS"))
    datetime.strptime(value, fmt)
    return value


class FirstMapRequest(BaseModel):
    date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    lat: float = Field(..., ge=-90, le=90)
    lng: float = Field(..., ge=-180, le=180)
    gender: str = Field(..., pattern="^(male|female)$")
    # Still accepted (the frontend sends it); the result is English-only and does not depend on it.
    lang: str = Field(default="en", pattern="^(en|pl)$")
    time_mode: str = Field(..., pattern="^(exact|approximate|unknown)$")
    time_exact: str | None = Field(default=None, description="HH:MM or HH:MM:SS when time_mode=exact")
    time_bucket: str | None = Field(
        default=None,
        description="Bucket id when time_mode=approximate (see ONBOARDING_UX.md)",
    )
    archetype_id: str | None = Field(
        default=None,
        description="When multiple archetypes fit the time window, the user-selected ArchetypeID",
    )
    birth_time: str | None = Field(
        default=None,
        description="HH:MM:SS from candidate picker — avoids rescanning the window",
    )

    @field_validator("date")
    @classmethod
    def _real_date(cls, v: str) -> str:
        date.fromisoformat(v)
        return v

    @model_validator(mode="after")
    def _time_exact_format(self):
        # Only read in exact mode; other modes ignore the field, as before.
        if self.time_mode == "exact" and self.time_exact:
            _check_time(self.time_exact.strip(), allow_hhmm=True)
        return self

    @field_validator("birth_time")
    @classmethod
    def _birth_time_format(cls, v: str | None) -> str | None:
        return _check_time(v, allow_hhmm=False)


class ArchetypeCandidateOut(BaseModel):
    archetype_id: str
    birth_time: str


class FirstMapCandidatesResponse(BaseModel):
    candidates: list[ArchetypeCandidateOut]
    choice_required: bool
    time_precision: str
    time_bucket: str | None = None


class FirstMapResponse(BaseModel):
    archetype_id: str
    ascendant: str
    ruler_planet: str
    ruler_sign: str
    ruler_house: int
    ruler_house_theme: str
    lang: str
    gender: str
    time_precision: str
    time_bucket: str | None = None


def _resolve_archetype_from_chart(chart: ChartData) -> tuple[pd.Series, str, str, str, int]:
    result = chart.get_ascendent_and_its_lord_signs()
    ascendant_pl = list(result.keys())[0]
    ruler_sign_pl = list(result.values())[0]
    ruler_planet_code = chart.AS_LORD.get(ascendant_pl, "")
    ruler_planet_name = chart.PLANET_NAMES.get(ruler_planet_code, ruler_planet_code)

    ascendant_en = ZODIAC_PL_TO_EN.get(ascendant_pl)
    ruler_sign_en = ZODIAC_PL_TO_EN.get(ruler_sign_pl)

    if not ascendant_en or not ruler_sign_en:
        raise _map_error(500, f"unknown sign AS={ascendant_pl} ruler_in={ruler_sign_pl}")

    match = archetypes_df[
        (archetypes_df["Ascendant"] == ascendant_en) &
        (archetypes_df["RulerSign"] == ruler_sign_en)
    ]

    if match.empty:
        raise _map_error(404, f"no archetype for AS={ascendant_en} RulerSign={ruler_sign_en}")

    row = match.iloc[0]

    try:
        ruler_house = int(chart.chart_data[ruler_planet_code][0])
    except (KeyError, TypeError, ValueError) as e:
        raise _map_error(500, f"ruler house unreadable for {ruler_planet_code}: {type(e).__name__}") from e
    if not 1 <= ruler_house <= 12:
        raise _map_error(500, f"ruler house out of range: {ruler_house}")

    return row, ascendant_en, ruler_planet_name, ruler_sign_en, ruler_house


def _make_chart(date: str, birth_time: str, lat: float, lng: float) -> ChartData:
    return ChartData(date=date, real_time=birth_time, latitude=lat, longitude=lng)


def _chart_failed(e: Exception) -> HTTPException:
    # Exception text can contain the submitted date/time, so only its type is logged.
    return _map_error(400, f"chart computation error: {type(e).__name__}")


def _row_to_candidate(row: pd.Series, *, birth_time: str) -> ArchetypeCandidateOut:
    return ArchetypeCandidateOut(
        archetype_id=str(row["ArchetypeID"]),
        birth_time=birth_time,
    )


def _scan_step_minutes(time_mode: str) -> int:
    return UNKNOWN_SCAN_STEP_MINUTES if time_mode == "unknown" else SCAN_STEP_MINUTES


def _collect_archetype_candidates(req: FirstMapRequest) -> tuple[list[ArchetypeCandidateOut], str]:
    if req.time_mode == "exact":
        try:
            birth_time, time_precision = resolve_birth_time(
                req.time_mode,  # type: ignore[arg-type]
                time_exact=req.time_exact,
                time_bucket=req.time_bucket,
            )
        except ValueError as e:
            raise _map_error(400, f"time input rejected: {e}") from e
        try:
            chart = _make_chart(req.date, birth_time, req.lat, req.lng)
            row, _, _, _, _ = _resolve_archetype_from_chart(chart)
        except HTTPException:
            raise
        except Exception as e:
            raise _chart_failed(e) from e
        return [_row_to_candidate(row, birth_time=birth_time)], time_precision

    try:
        window = resolve_time_window(req.time_mode, time_bucket=req.time_bucket)  # type: ignore[arg-type]
    except ValueError as e:
        raise _map_error(400, f"time input rejected: {e}") from e
    if window is None:
        raise _map_error(400, "invalid time window")

    time_precision = "approximate" if req.time_mode == "approximate" else "low"
    start, end = window
    seen: dict[str, ArchetypeCandidateOut] = {}
    step = _scan_step_minutes(req.time_mode)

    for birth_time in iter_times_in_window(req.date, start, end, step_minutes=step):
        try:
            chart = _make_chart(req.date, birth_time, req.lat, req.lng)
            row, _, _, _, _ = _resolve_archetype_from_chart(chart)
        except HTTPException:
            continue
        except Exception:
            continue
        archetype_id = str(row["ArchetypeID"])
        if archetype_id in seen:
            continue
        seen[archetype_id] = _row_to_candidate(row, birth_time=birth_time)

    if not seen:
        raise _map_error(404, "no archetype in the time window")

    return list(seen.values()), time_precision


def _find_birth_time_for_archetype(req: FirstMapRequest, archetype_id: str) -> str | None:
    try:
        window = resolve_time_window(req.time_mode, time_bucket=req.time_bucket)  # type: ignore[arg-type]
    except ValueError:
        return None
    if window is None:
        return None
    start, end = window
    step = _scan_step_minutes(req.time_mode)
    for birth_time in iter_times_in_window(req.date, start, end, step_minutes=step):
        try:
            chart = _make_chart(req.date, birth_time, req.lat, req.lng)
            row, _, _, _, _ = _resolve_archetype_from_chart(chart)
        except HTTPException:
            continue
        except Exception:
            continue
        if str(row["ArchetypeID"]) == archetype_id:
            return birth_time
    return None


def _build_first_map_response(
    req: FirstMapRequest,
    row: pd.Series,
    ascendant_en: str,
    ruler_planet_name: str,
    ruler_sign_en: str,
    ruler_house: int,
    time_precision: str,
) -> FirstMapResponse:
    return FirstMapResponse(
        archetype_id=str(row["ArchetypeID"]),
        ascendant=ascendant_en,
        ruler_planet=ruler_planet_name,
        ruler_sign=ruler_sign_en,
        ruler_house=ruler_house,
        ruler_house_theme=archetype_ruler_house_theme(ruler_house),
        lang=req.lang,
        gender=req.gender,
        time_precision=time_precision,
        time_bucket=req.time_bucket,
    )


@app.post("/first-map/candidates", response_model=FirstMapCandidatesResponse)
def post_first_map_candidates(req: FirstMapRequest):
    candidates, time_precision = _collect_archetype_candidates(req)
    return FirstMapCandidatesResponse(
        candidates=candidates,
        choice_required=len(candidates) > 1,
        time_precision=time_precision,
        time_bucket=req.time_bucket,
    )


@app.post("/first-map", response_model=FirstMapResponse)
def post_first_map(req: FirstMapRequest):
    if req.time_mode in ("approximate", "unknown"):
        time_precision = "approximate" if req.time_mode == "approximate" else "low"

        if req.archetype_id and req.birth_time:
            try:
                chart = _make_chart(req.date, req.birth_time, req.lat, req.lng)
                row, ascendant_en, ruler_planet_name, ruler_sign_en, ruler_house = _resolve_archetype_from_chart(chart)
            except HTTPException:
                raise
            except Exception as e:
                raise _chart_failed(e) from e
            if str(row["ArchetypeID"]) != req.archetype_id:
                raise _map_error(400, "birth_time does not match archetype_id")
            return _build_first_map_response(
                req, row, ascendant_en, ruler_planet_name, ruler_sign_en, ruler_house, time_precision
            )

        candidates, time_precision = _collect_archetype_candidates(req)
        if len(candidates) > 1:
            if not req.archetype_id:
                raise _map_error(400, "archetype_id is required when multiple patterns fit the time window")
            if req.archetype_id not in {c.archetype_id for c in candidates}:
                raise _map_error(400, "invalid archetype_id for this time window")
            birth_time = req.birth_time
            if birth_time:
                try:
                    chart = _make_chart(req.date, birth_time, req.lat, req.lng)
                    row, ascendant_en, ruler_planet_name, ruler_sign_en, ruler_house = _resolve_archetype_from_chart(chart)
                except Exception as e:
                    raise _chart_failed(e) from e
                if str(row["ArchetypeID"]) != req.archetype_id:
                    raise _map_error(400, "birth_time does not match archetype_id")
            else:
                birth_time = _find_birth_time_for_archetype(req, req.archetype_id)
                if not birth_time:
                    raise _map_error(404, "archetype_id not found in time window")
                chart = _make_chart(req.date, birth_time, req.lat, req.lng)
                row, ascendant_en, ruler_planet_name, ruler_sign_en, ruler_house = _resolve_archetype_from_chart(chart)
        elif len(candidates) == 1:
            only = candidates[0]
            if req.archetype_id and req.archetype_id != only.archetype_id:
                raise _map_error(400, "invalid archetype_id for this time window")
            birth_time = req.birth_time or only.birth_time
            try:
                chart = _make_chart(req.date, birth_time, req.lat, req.lng)
            except Exception as e:
                raise _chart_failed(e) from e
            row, ascendant_en, ruler_planet_name, ruler_sign_en, ruler_house = _resolve_archetype_from_chart(chart)
        else:
            raise _map_error(404, "no archetype in the time window")

        return _build_first_map_response(
            req, row, ascendant_en, ruler_planet_name, ruler_sign_en, ruler_house, time_precision
        )

    try:
        birth_time, time_precision = resolve_birth_time(
            req.time_mode,  # type: ignore[arg-type]
            time_exact=req.time_exact,
            time_bucket=req.time_bucket,
        )
    except ValueError as e:
        raise _map_error(400, f"time input rejected: {e}") from e

    try:
        chart = _make_chart(req.date, birth_time, req.lat, req.lng)
    except Exception as e:
        raise _chart_failed(e) from e

    row, ascendant_en, ruler_planet_name, ruler_sign_en, ruler_house = _resolve_archetype_from_chart(chart)

    if req.archetype_id and str(row["ArchetypeID"]) != req.archetype_id:
        raise _map_error(400, "archetype_id does not match exact birth time")

    return _build_first_map_response(
        req, row, ascendant_en, ruler_planet_name, ruler_sign_en, ruler_house, time_precision
    )


@app.get("/health")
def health():
    return {"status": "ok", "archetypes_loaded": len(archetypes_df)}


# --- AGPL source offer ---------------------------------------------------------------------------
# This service runs Swiss Ephemeris (pyswisseph, AGPL). The exact files of the deployed image are
# published on every deploy (.github/workflows/deploy.yml, job publish-source). The image build
# writes the content hash of its files (source_manifest.py); the mirror commit carries the same hash.
SOURCE_REPOSITORY = "https://github.com/AYKALEN/aykalen-api-source"
SOURCE_HASH_FILE = os.getenv("SOURCE_HASH_FILE", "/source-sha256.txt")


def _source_hash() -> str | None:
    try:
        with open(SOURCE_HASH_FILE, encoding="ascii") as f:
            return f.read().strip() or None
    except OSError:
        return None


_SOURCE_SHA256 = _source_hash()


@app.get("/source")
def source():
    return {
        "repository": SOURCE_REPOSITORY,
        "license": "AGPL-3.0-or-later",
        "engine": "https://github.com/AYKALEN/aykalen-astrology-engine",
        "content_sha256": _SOURCE_SHA256,
    }


# --- Mobile app: full calculation ---------------------------------------------------------------
# One engine run takes seconds, so it runs in a single separate worker process: the site's
# /first-map keeps its own CPU time. At most APP_CALC_MAX_IN_FLIGHT requests are accepted at once
# (one running, the rest queued); more get 503 + Retry-After. Birth data is never logged.

APP_ERROR = "Could not calculate the map for these details."
APP_BUSY = "Calculation capacity is temporarily unavailable."
APP_RATE = "Too many calculations from this network. Try again later."
APP_CALC_MAX_IN_FLIGHT = int(os.getenv("APP_CALC_MAX_IN_FLIGHT", "2"))
APP_CALC_TIMEOUT_S = float(os.getenv("APP_CALC_TIMEOUT_S", "60"))
APP_CALC_PER_IP_PER_HOUR = int(os.getenv("APP_CALC_PER_IP_PER_HOUR", "30"))

_app_pool: ProcessPoolExecutor | None = None
_app_pool_lock = threading.Lock()
_app_in_flight = 0
_app_flight_lock = threading.Lock()
_app_ip_hits: dict[str, deque] = {}
_app_ip_lock = threading.Lock()
_app_tz: TimezoneFinder | None = None


def _app_executor() -> ProcessPoolExecutor:
    global _app_pool
    with _app_pool_lock:
        if _app_pool is None:
            # spawn, not fork: the parent runs threads (uvicorn), and fork with threads can hang.
            _app_pool = ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"))
        return _app_pool


def _app_discard_executor(pool: ProcessPoolExecutor) -> None:
    """Kill this pool's worker and drop the pool, unless another request already replaced it.

    Used after a timeout (the running task cannot be cancelled otherwise and would block every
    later request) and after the worker died. The next request starts a fresh worker.
    """
    global _app_pool
    with _app_pool_lock:
        if _app_pool is not pool:
            return
        _app_pool = None
    for process in list((getattr(pool, "_processes", None) or {}).values()):
        process.terminate()
    pool.shutdown(wait=False, cancel_futures=True)


def _app_timezone(lat: float, lng: float) -> str:
    # The same resolution the site uses (engine.ChartData): TimezoneFinder, else UTC.
    global _app_tz
    if _app_tz is None:
        _app_tz = TimezoneFinder()
    return _app_tz.timezone_at(lat=lat, lng=lng) or "UTC"


def _app_client_key(request: Request) -> str | None:
    """Client IP set by the reverse proxy; None when the proxy sends no client address.

    X-Real-IP is set by nginx itself. With X-Forwarded-For only the last hop is trusted (the one
    nginx appended); earlier hops can be forged by the client.
    """
    real = request.headers.get("x-real-ip", "").strip()
    if real:
        return real
    forwarded = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
    return forwarded[-1] if forwarded else None


def _app_rate_limited(key: str | None, now: float) -> int:
    """Seconds to wait when this client used up its hourly calculations, else 0."""
    if key is None or APP_CALC_PER_IP_PER_HOUR <= 0:
        return 0
    with _app_ip_lock:
        hits = _app_ip_hits.setdefault(key, deque())
        while hits and now - hits[0] >= 3600:
            hits.popleft()
        if len(hits) >= APP_CALC_PER_IP_PER_HOUR:
            return int(3600 - (now - hits[0])) + 1
        hits.append(now)
        if len(_app_ip_hits) > 10000:  # bound memory: drop clients with no hits in the window
            for k in [k for k, v in _app_ip_hits.items() if not v or now - v[-1] >= 3600]:
                _app_ip_hits.pop(k, None)
        return 0


def _app_error(status_code: int, detail: str, retry_after: int | None = None) -> JSONResponse:
    headers = {"Cache-Control": "no-store"}
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return JSONResponse(status_code=status_code, content={"detail": detail}, headers=headers)


class AppCalculationRequest(BaseModel):
    """Same birth fields as /first-map. For approximate/unknown time the app first calls
    /first-map/candidates and sends the chosen candidate's birth_time here."""

    date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    lat: float = Field(..., ge=-90, le=90)
    lng: float = Field(..., ge=-180, le=180)
    time_mode: str = Field(..., pattern="^(exact|approximate|unknown)$")
    time_exact: str | None = None
    time_bucket: str | None = None
    birth_time: str | None = None

    @field_validator("date")
    @classmethod
    def _real_date(cls, v: str) -> str:
        if not 1900 <= date.fromisoformat(v).year <= date.today().year:
            raise ValueError("date out of range")
        return v

    @field_validator("birth_time")
    @classmethod
    def _birth_time_format(cls, v: str | None) -> str | None:
        return _check_time(v, allow_hhmm=False)

    @model_validator(mode="after")
    def _time_fields(self):
        if self.time_mode == "exact":
            if not self.time_exact:
                raise ValueError("time_exact is required")
            _check_time(self.time_exact.strip(), allow_hhmm=True)
        return self


def _app_birth_time(req: AppCalculationRequest) -> tuple[str, str]:
    """(HH:MM:SS, time_precision) with the site's rules. Raises ValueError on bad combinations."""
    if req.time_mode == "exact":
        return resolve_birth_time("exact", time_exact=req.time_exact)
    if req.time_mode == "approximate":
        window = resolve_time_window("approximate", time_bucket=req.time_bucket)
        if req.birth_time:
            if not window[0] <= req.birth_time <= window[1]:
                raise ValueError("birth_time outside the bucket")
            return req.birth_time, "approximate"
        return resolve_birth_time("approximate", time_bucket=req.time_bucket)
    if req.birth_time:
        return req.birth_time, "low"
    return resolve_birth_time("unknown")


@app.post("/v1/app/calculation")
async def post_app_calculation(req: AppCalculationRequest, request: Request):
    global _app_in_flight
    try:
        birth_time, time_precision = _app_birth_time(req)
    except ValueError:
        logger.info("app calculation rejected: time fields")
        return _app_error(422, APP_ERROR)
    if abs(req.lat) > app_calculation.MAX_ABS_LATITUDE:
        logger.info("app calculation rejected: latitude outside the supported range")
        return _app_error(422, APP_ERROR)

    # Admission first, so "busy" answers do not use up the client's hourly calculations.
    with _app_flight_lock:
        if _app_in_flight >= APP_CALC_MAX_IN_FLIGHT:
            logger.info("app calculation busy")
            return _app_error(503, APP_BUSY, retry_after=10)
        _app_in_flight += 1
    try:
        wait = _app_rate_limited(_app_client_key(request), time.monotonic())
        if wait:
            logger.info("app calculation rate-limited")
            return _app_error(429, APP_RATE, retry_after=wait)
        tz_name = _app_timezone(req.lat, req.lng)
        pool = _app_executor()
        try:
            future = pool.submit(app_calculation.compute, req.date, birth_time, tz_name, req.lat, req.lng)
        except (BrokenProcessPool, RuntimeError):
            # The worker died while idle, or another request is restarting it right now.
            logger.error("app calculation worker unavailable; restarting the pool")
            _app_discard_executor(pool)
            return _app_error(503, APP_BUSY, retry_after=10)
        try:
            calculation = await asyncio.wait_for(asyncio.wrap_future(future), APP_CALC_TIMEOUT_S)
        except asyncio.TimeoutError:
            logger.error("app calculation timed out; restarting the worker")
            _app_discard_executor(pool)
            return _app_error(503, APP_BUSY, retry_after=30)
        except BrokenProcessPool:
            logger.error("app calculation worker stopped; restarting the pool")
            _app_discard_executor(pool)
            return _app_error(503, APP_BUSY, retry_after=10)
        except asyncio.CancelledError:
            # Our queued task was cancelled because another request restarted the worker
            # (a client disconnect is re-raised as usual).
            if future.cancelled() and _app_pool is not pool:
                return _app_error(503, APP_BUSY, retry_after=10)
            raise
        except app_calculation.CalculationUnavailable as e:
            logger.info("app calculation unavailable: %s", e)
            return _app_error(422, APP_ERROR)
        except Exception as e:
            # Only the type: messages from the engine can contain input values.
            logger.error("app calculation failed: %s", type(e).__name__)
            return _app_error(500, APP_ERROR)
    finally:
        with _app_flight_lock:
            _app_in_flight -= 1

    return JSONResponse(
        content={
            "schema_version": "aykalen-app-calculation-1",
            "time_precision": time_precision,
            "timezone": tz_name,
            "calculation": calculation,
        },
        headers={"Cache-Control": "no-store"},
    )


@app.post("/v1/app/candidates")
def post_app_candidates(req: AppCalculationRequest, request: Request):
    """Archetype hypotheses for an approximate or unknown birth time (the site's scan, no gender).

    The app shows them, the person picks one, and sends its birth_time to /v1/app/calculation.
    Uses the same hourly quota per client as the calculation. Runs in FastAPI's thread pool.
    """
    if req.time_mode == "exact" or abs(req.lat) > app_calculation.MAX_ABS_LATITUDE:
        logger.info("app candidates rejected: mode or latitude")
        return _app_error(422, APP_ERROR)
    try:
        resolve_time_window(req.time_mode, time_bucket=req.time_bucket)  # type: ignore[arg-type]
    except ValueError:
        logger.info("app candidates rejected: time window")
        return _app_error(422, APP_ERROR)
    wait = _app_rate_limited(_app_client_key(request), time.monotonic())
    if wait:
        logger.info("app candidates rate-limited")
        return _app_error(429, APP_RATE, retry_after=wait)
    scan = FirstMapRequest(date=req.date, lat=req.lat, lng=req.lng, gender="female",  # gender unused by the scan
                           time_mode=req.time_mode, time_bucket=req.time_bucket)
    try:
        candidates, time_precision = _collect_archetype_candidates(scan)
    except HTTPException:
        # The scan logged the reason as "first-map failed: ..." (no input values).
        logger.info("app candidates failed: scan")
        return _app_error(422, APP_ERROR)
    return JSONResponse(
        content={
            "schema_version": "aykalen-app-candidates-1",
            "time_precision": time_precision,
            "candidates": [c.model_dump() for c in candidates],
        },
        headers={"Cache-Control": "no-store"},
    )
