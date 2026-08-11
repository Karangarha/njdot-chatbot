"""North/South-of-I-195 classification from key map coordinates.

NJDOT's regional completion deadlines (see ``substantial_regional_deadlines``
in ``app.compliance.catalog``) hinge on whether a project lies north or south
of I-195, which runs roughly east-west across the state from the Delaware
River (Trenton/Hamilton) to the shore (Wall Twp/Belmar). The key map sheet
prints the project's mid-point latitude/longitude, so the classification is a
pure coordinate comparison against a hardcoded polyline of the actual I-195
alignment — no LLM judgement, no external API.

``GEO_REGION_VALIDATION`` (config) selects the method; only ``"hardcoded"``
is implemented. ``"hybrid_google"`` is a reserved seam for a later
Google-Geocoding sanity check (``GOOGLE_MAPS_API_KEY``) and currently falls
through to hardcoded with a warning.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass
from typing import Any, List, Optional, Tuple

from app.config import config

logger = logging.getLogger(__name__)

# New Jersey bounding box (generous). Coordinates outside it are treated as a
# mis-extraction (``suspect``) rather than classified.
NJ_LAT_MIN, NJ_LAT_MAX = 38.90, 41.40
NJ_LON_MIN, NJ_LON_MAX = -75.60, -73.85

# Within this many degrees of latitude of the interpolated I-195 line, the
# call is flagged ``borderline`` (~1.7 km — the corridor's own municipalities
# are genuinely ambiguous at key-map precision).
BORDERLINE_EPSILON_DEG = 0.015

# I-195 alignment, ordered west -> east as (lat, lon) vertices traced from
# the actual route. The line dips south through Jackson/Howell (~40.155°N)
# before rising back toward the shore terminus, so a single latitude
# threshold would misclassify mid-state projects near the corridor.
I195_POLYLINE: List[Tuple[float, float]] = [
    (40.192, -74.740),  # Western terminus at NJ 29, Delaware River (Trenton/Hamilton line)
    (40.190, -74.715),  # I-295 interchange, Hamilton Twp
    (40.183, -74.660),  # Yardville / Hamilton Square area
    (40.197, -74.610),  # Robbinsville Twp, west of the Turnpike
    (40.208, -74.560),  # NJ Turnpike interchange 7A, Robbinsville
    (40.196, -74.523),  # CR 539 crossing, Upper Freehold (Allentown)
    (40.186, -74.485),  # Imlaystown / Cox's Corner area
    (40.176, -74.452),  # Millstone Twp, near Clarksburg
    (40.160, -74.440),  # CR 537 / Six Flags Great Adventure, Jackson
    (40.155, -74.360),  # Jackson Mills / CR 527
    (40.155, -74.250),  # US 9 crossing, Howell
    (40.170, -74.185),  # Farmingdale / CR 547
    (40.167, -74.095),  # Garden State Parkway interchange, Wall Twp
    (40.172, -74.062),  # Eastern terminus at NJ 34/NJ 138 (toward Belmar)
]

# Direction letter standing alone (word-bounded) so the "S" in a run like
# "26S" inside a DMS string with unit letters doesn't read as "South".
_DIRECTION_RE = re.compile(r"\b([NSEW])\b")
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


@dataclass
class RegionResult:
    """Outcome of the north/south-of-I-195 classification, with enough
    intermediate detail to serve as check evidence verbatim."""

    region: Optional[str]           # "NORTH" | "SOUTH" | None (unresolvable)
    method: str                     # "hardcoded_i195_polyline" (future: "hybrid_google")
    latitude: Optional[float]
    longitude: Optional[float]
    i195_lat_at_lon: Optional[float]
    delta_deg: Optional[float]      # project latitude minus interpolated I-195 latitude
    suspect: bool                   # coordinates fall outside the NJ bounding box
    borderline: bool                # |delta_deg| < BORDERLINE_EPSILON_DEG
    detail: str                     # human-readable evidence sentence

    def as_dict(self) -> dict:
        return asdict(self)


def parse_coordinate(raw: Optional[str], *, is_longitude: bool = False) -> Optional[float]:
    """Parse a latitude/longitude string to signed decimal degrees.

    Handles the formats real key sheets print: decimal (``40.2239``), DMS
    with any separators — including mojibake degree signs (``39�23'43"N``) —
    and N/S/E/W suffixes. NJ longitudes printed unsigned/W-suffixed are
    normalized to negative.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None

    nums = _NUMBER_RE.findall(s)
    if not nums:
        return None
    value = float(nums[0])
    if len(nums) > 1:
        value += float(nums[1]) / 60.0
    if len(nums) > 2:
        value += float(nums[2]) / 3600.0

    direction_match = _DIRECTION_RE.search(s.upper())
    direction = direction_match.group(1) if direction_match else None
    if direction in ("S", "W") or (direction is None and s.startswith("-")):
        value = -value
    # Key sheets commonly print longitude unsigned with no usable direction
    # letter; anything in the 73–76 range in an NJ document is degrees West.
    if is_longitude and 73.0 <= value <= 76.0:
        value = -value
    return value


def interpolate_i195_lat(lon: float) -> float:
    """Latitude of the I-195 line at ``lon``, linearly interpolated between
    the bracketing polyline vertices. Longitudes beyond the polyline's span
    (Camden, Cape May, the shore) clamp to the nearest endpoint's latitude —
    NJDOT's "north/south of I-195" convention extends the line to the state
    borders at its terminal latitude.
    """
    if lon <= I195_POLYLINE[0][1]:
        return I195_POLYLINE[0][0]
    if lon >= I195_POLYLINE[-1][1]:
        return I195_POLYLINE[-1][0]
    for (lat_w, lon_w), (lat_e, lon_e) in zip(I195_POLYLINE, I195_POLYLINE[1:]):
        if lon_w <= lon <= lon_e:
            if lon_e == lon_w:
                return lat_w
            frac = (lon - lon_w) / (lon_e - lon_w)
            return lat_w + frac * (lat_e - lat_w)
    return I195_POLYLINE[-1][0]  # unreachable given the checks above


def classify_north_south(lat: float, lon: float) -> RegionResult:
    """Classify a point as NORTH or SOUTH of I-195 (hardcoded polyline)."""
    if not (NJ_LAT_MIN <= lat <= NJ_LAT_MAX and NJ_LON_MIN <= lon <= NJ_LON_MAX):
        return RegionResult(
            region=None, method="hardcoded_i195_polyline",
            latitude=lat, longitude=lon,
            i195_lat_at_lon=None, delta_deg=None,
            suspect=True, borderline=False,
            detail=(
                f"Key map coordinates ({lat:.4f}, {lon:.4f}) fall outside New Jersey "
                f"(lat {NJ_LAT_MIN}–{NJ_LAT_MAX}, lon {NJ_LON_MIN}–{NJ_LON_MAX}) — "
                "likely a coordinate extraction error; manual verification needed."
            ),
        )

    i195_lat = interpolate_i195_lat(lon)
    delta = lat - i195_lat
    region = "NORTH" if delta > 0 else "SOUTH"
    borderline = abs(delta) < BORDERLINE_EPSILON_DEG
    detail = (
        f"Key map coordinates ({lat:.4f}, {lon:.4f}) are {region} of I-195 "
        f"(interpolated I-195 latitude {i195_lat:.3f} at longitude {lon:.3f}; "
        f"delta {delta:+.3f}°, ~{abs(delta) * 111.0:.0f} km). "
        f"Method: hardcoded_i195_polyline."
    )
    if borderline:
        detail += (
            " NOTE: the project sits within ~1.7 km of the I-195 corridor — "
            "the north/south call is borderline and worth a manual confirm."
        )
    return RegionResult(
        region=region, method="hardcoded_i195_polyline",
        latitude=lat, longitude=lon,
        i195_lat_at_lon=round(i195_lat, 4), delta_deg=round(delta, 4),
        suspect=False, borderline=borderline, detail=detail,
    )


def resolve_region(extraction: Any) -> RegionResult:
    """Resolve the project's I-195 region from a ``KeyMapExtraction``
    (duck-typed to avoid importing ``app.ingestion.keymap_extractor``, which
    imports this module).

    Python-parsed ``latitude_raw``/``longitude_raw`` strings win over the
    LLM's decimal conversions (LLMs botch DMS arithmetic).
    """
    method = getattr(config, "GEO_REGION_VALIDATION", "hardcoded")
    if method not in ("", "hardcoded"):
        logger.warning(
            "GEO_REGION_VALIDATION=%r is not implemented; falling back to hardcoded", method,
        )

    lat = parse_coordinate(getattr(extraction, "latitude_raw", None))
    lon = parse_coordinate(getattr(extraction, "longitude_raw", None), is_longitude=True)
    if lat is None:
        lat = getattr(extraction, "latitude_decimal", None)
    if lon is None:
        lon = getattr(extraction, "longitude_decimal", None)
        if lon is not None and 73.0 <= lon <= 76.0:
            lon = -lon

    if lat is None or lon is None:
        missing = [n for n, v in (("latitude", lat), ("longitude", lon)) if v is None]
        return RegionResult(
            region=None, method="hardcoded_i195_polyline",
            latitude=lat, longitude=lon,
            i195_lat_at_lon=None, delta_deg=None,
            suspect=False, borderline=False,
            detail=f"Could not find or parse the project {' and '.join(missing)} on the key map.",
        )
    return classify_north_south(lat, lon)
