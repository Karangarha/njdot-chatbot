"""In-process memoization so the same uploaded schedule isn't parsed and run
through the CPM engine twice.

The frontend fires ``POST /api/review`` and ``POST /api/session/upload`` in
parallel with the *same* ``.xer`` bytes — each independently used to call
``parse_xer_all`` → ``build_network``/``build_calendars`` → ``run_cpm`` →
``cross_check``. That work is deterministic given the file bytes, so this
module caches it by a hash of the raw bytes and lets both callers share one
computation.

Concurrency: review runs synchronously on the event-loop thread while session
ingestion runs in a background thread, so both can legitimately race to
compute the same key. A per-key lock plus double-checked read makes the loser
of the race simply wait for and reuse the winner's result instead of
recomputing.

Testing-scale only: the cache is a small bounded LRU (in-memory, per-process,
not persisted) — fine for local/dev use where a handful of distinct schedules
are in flight. Each entry lives only for this process's lifetime.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from collections import OrderedDict
from typing import Any, Dict

from app.scheduling.calendar import build_calendars
from app.scheduling.cpm import build_network, run_cpm
from app.scheduling.crosscheck import cross_check
from app.scheduling.xer_extract import parse_xer_all

logger = logging.getLogger(__name__)

_MAX_ENTRIES = 16

_cache: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_cache_guard = threading.Lock()          # protects _cache and _key_locks structure
_key_locks: Dict[str, threading.Lock] = {}


def _lock_for(key: str) -> threading.Lock:
    with _cache_guard:
        lock = _key_locks.get(key)
        if lock is None:
            lock = _key_locks[key] = threading.Lock()
        return lock


def compute_cpm_cached(xer_bytes: bytes) -> Dict[str, Any]:
    """Parse + run CPM for these exact .xer bytes, computing at most once.

    Returns a dict with ``schedule_json``, ``calendars``, ``project``, ``cpm``
    (``None`` if the CPM pass failed — non-fatal, callers degrade gracefully),
    ``xcheck``, and ``from_cache`` (``True`` when this call reused a prior
    computation instead of parsing/running CPM again — useful in timing logs).
    """
    key = hashlib.sha256(xer_bytes).hexdigest()

    with _cache_guard:
        hit = _cache.get(key)
        if hit is not None:
            _cache.move_to_end(key)   # LRU touch
            return {**hit, "from_cache": True}

    # Not cached yet — take this key's lock so only one caller computes it;
    # a concurrent caller blocks here and then reuses the result below.
    with _lock_for(key):
        with _cache_guard:
            hit = _cache.get(key)
            if hit is not None:
                _cache.move_to_end(key)
                return {**hit, "from_cache": True}

        xer_text = xer_bytes.decode("utf-8", errors="ignore")
        parsed = parse_xer_all(xer_text)
        schedule_json = parsed["activities"]
        calendars = parsed["calendars"]
        project = parsed["project"]

        cpm = xcheck = None
        try:
            cpm = run_cpm(build_network(schedule_json), build_calendars(calendars), project)
            xcheck = cross_check(schedule_json, cpm)
        except Exception:
            logger.exception("CPM computation failed; result cached without computed values")

        result = {
            "schedule_json": schedule_json,
            "calendars": calendars,
            "project": project,
            "cpm": cpm,
            "xcheck": xcheck,
        }

        with _cache_guard:
            _cache[key] = result
            _cache.move_to_end(key)
            while len(_cache) > _MAX_ENTRIES:
                evicted_key, _ = _cache.popitem(last=False)
                _key_locks.pop(evicted_key, None)

        return {**result, "from_cache": False}
