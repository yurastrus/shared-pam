# SPDX-License-Identifier: AGPL-3.0-only
"""Simultaneous detections: a conservative lower bound on calling individuals.

PAM does not count individuals. A recorder reports "the species is present",
not "how many". Counting acoustically needs dense arrays with tight clock
synchronisation so a call can be trilaterated, which is expensive.

What the existing data does support: if the same species is detected at N
spatially separated locations inside one short time window, then at least N
individuals were calling in that window, provided the locations are far enough
apart that a single loud individual cannot reach two recorders. Repeating that
over every window in a period gives a distribution of N, which is a cheap
signal about how many individuals the network is listening to.

The result is a LOWER BOUND, not an estimate of abundance. It says nothing
about individuals that stayed silent.

Two things the design takes seriously:

1. Pseudo-replication. One loud bird heard by two recorders 150 m apart looks
   like two individuals. ``min_distance_m`` is the distance below which two
   detections are not counted separately; ``cluster_method`` says how the rule
   is enforced (see ``count_windows``).
2. Classifier error. A false positive at two distant points in the same minute
   fabricates a "simultaneous pair", so the confidence threshold is a
   first-class parameter. This module takes a RAW confidence; reading it off the
   species logistic precision fit in ``evaluation`` (as ``/pam/pam_detailed``
   does) is a later step, deliberately not done here.

The numeric core is Flask-free and takes an open SQLAlchemy connection, so it
is testable without an app context.
"""

from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict

import numpy as np
from pyproj import CRS, Transformer
from sqlalchemy import text

# --------------------------------------------------------------------------
# Defaults and limits. The UI shows these; the API re-clamps them, because a
# request never has to come from the UI.
# --------------------------------------------------------------------------

#: Score columns on ``detections`` that may be used. Whitelisted because the
#: column name is interpolated into SQL.
CONF_COLUMNS = ("confidence", "conf_perch_v2")

DEFAULT_WINDOW_S = 60
DEFAULT_MIN_DISTANCE_M = 500.0
DEFAULT_MIN_CONF = 0.95
DEFAULT_CLUSTER_METHOD = "greedy"
CLUSTER_METHODS = ("greedy", "complete", "single")

#: Window width bounds for the UI's numeric field, in seconds. Below 10 s the
#: recorder clock error stops being negligible; above an hour "simultaneous"
#: has no biological meaning left.
WINDOW_MIN_S = 10
WINDOW_MAX_S = 3600

#: Windows returned with full per-location detail, so the map can switch
#: between them without another query. The CSV export is not limited.
DETAIL_WINDOWS = 50

#: The Top-windows table lists only windows reaching this many well-separated
#: locations. A single location says nothing about simultaneity, however many
#: detections it holds: one bird singing twelve times in a minute is still one
#: bird. The histogram and the KPI tiles still cover every window, because the
#: shape of the whole distribution is what shows how rare the co-occurrences
#: are; and when nothing in the selection reaches the threshold the list falls
#: back to single-location windows rather than going blank, with a flag saying
#: so.
TABLE_MIN_COUNTED = 2

#: Statement timeout for every query this module issues.
STATEMENT_TIMEOUT_MS = 30_000

#: Overlapping windows cost one database pass per offset; refuse absurd ones.
MAX_DB_PASSES = 12

#: The sweep scans the raw event list in Python once per candidate width, so
#: its cost is (events x widths). Far below the CLI prototype's 400k, which was
#: tuned for a script nobody waits on.
SWEEP_MAX_EVENTS = 20_000
SWEEP_MAX_WIDTHS = 120

DEFAULT_SWEEP_MIN_S = 10
DEFAULT_SWEEP_MAX_S = 600
DEFAULT_SWEEP_STEP_S = 10

#: Detection time is reconstructed from ``recordings.datetime_start +
#: detections.start_s``, so a recording that began before the period can still
#: carry detections inside it. The window query therefore reaches this far back
#: in ``datetime_start`` and filters on the reconstructed time afterwards.
RECORDING_SLACK = "1 day"


class CooccurrenceError(Exception):
    """A request that cannot be served, with a message meant for the user."""


# --------------------------------------------------------------------------
# Spatial helpers
# --------------------------------------------------------------------------

def pick_utm_crs(lons, lats) -> CRS:
    """UTM zone containing the centroid of the point set (WGS 84 datum).

    Distances are the whole argument here, so they are measured in a projected
    CRS in metres rather than in degrees.
    """
    lon = float(np.mean(lons))
    lat = float(np.mean(lats))
    zone = int((lon + 180.0) // 6.0) + 1
    epsg = (32600 if lat >= 0 else 32700) + zone
    return CRS.from_epsg(epsg)


def project(lons, lats, crs: CRS):
    tr = Transformer.from_crs(CRS.from_epsg(4326), crs, always_xy=True)
    x, y = tr.transform(np.asarray(lons, float), np.asarray(lats, float))
    return np.asarray(x), np.asarray(y)


def greedy_spaced_subset(items, xy, min_distance_m: float):
    """Largest-first greedy subset of points pairwise at least ``min_distance_m`` apart.

    ``items`` is ordered by importance (most detections first); ``xy`` maps a
    key to (x, y) in metres. Returns the accepted keys in input order.

    This is the operation the argument actually needs: if k points in one
    window are all mutually farther apart than the audible radius, at least k
    individuals were calling. Applied per window, so no location is discarded a
    priori. Being greedy it can return fewer points than the true maximum
    independent set, which keeps the answer a lower bound.
    """
    accepted: list = []
    d2 = float(min_distance_m) ** 2
    for key in items:
        px, py = xy[key]
        if all((px - xy[a][0]) ** 2 + (py - xy[a][1]) ** 2 >= d2 for a in accepted):
            accepted.append(key)
    return accepted


def cluster_locations_single(location_ids, x, y, min_distance_m: float):
    """Single-linkage connected components at ``min_distance_m``.

    Kept for comparison only. In a dense network it chains: at 1000 m the
    Розточчя network falls from 61 locations to 4 clusters, which destroys the
    signal.
    """
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    location_ids = list(location_ids)
    n = len(location_ids)
    if n == 0:
        return np.array([], int), {}
    if min_distance_m <= 0:
        labels = np.arange(n)
    else:
        x = np.asarray(x, float)
        y = np.asarray(y, float)
        dx = x[:, None] - x[None, :]
        dy = y[:, None] - y[None, :]
        close = (dx * dx + dy * dy) <= min_distance_m ** 2
        np.fill_diagonal(close, False)
        i, j = np.nonzero(close)
        adj = coo_matrix((np.ones(len(i)), (i, j)), shape=(n, n))
        _, labels = connected_components(adj, directed=False)
    return np.asarray(labels, int), {lid: int(lab) for lid, lab in zip(location_ids, labels)}


def cluster_locations_complete(location_ids, x, y, min_distance_m: float):
    """Complete-linkage clustering: every cluster has a diameter below the distance.

    Bounds the cluster diameter instead of chaining, so a cluster really is
    "one place where a single individual could be heard".
    """
    from scipy.cluster.hierarchy import fcluster, linkage
    from scipy.spatial.distance import pdist

    location_ids = list(location_ids)
    n = len(location_ids)
    if n == 0:
        return np.array([], int), {}
    if n == 1 or min_distance_m <= 0:
        labels = np.arange(n)
    else:
        z = linkage(pdist(np.column_stack([np.asarray(x, float), np.asarray(y, float)])),
                    method="complete")
        labels = fcluster(z, t=min_distance_m, criterion="distance") - 1
    return np.asarray(labels, int), {lid: int(lab) for lid, lab in zip(location_ids, labels)}


def close_matrix(x, y, min_distance_m: float) -> np.ndarray:
    """``close[i, j]`` is True when i and j are too near to count as two individuals."""
    n = len(x)
    if min_distance_m <= 0:
        return np.zeros((n, n), bool)
    xa = np.asarray(x, float)
    ya = np.asarray(y, float)
    dx = xa[:, None] - xa[None, :]
    dy = ya[:, None] - ya[None, :]
    close = (dx * dx + dy * dy) < min_distance_m ** 2
    np.fill_diagonal(close, False)
    return close


def independent_set(indices, close: np.ndarray) -> list[int]:
    """Greedy well-separated subset, fewest conflicts first.

    Maximum independent set is NP-hard, so this is a heuristic; ordering by
    ascending conflict count gives a larger subset than ordering by detection
    count, and any greedy answer is still a valid lower bound.
    """
    idx = list(dict.fromkeys(int(i) for i in indices))
    if not idx or not close.any():
        return sorted(idx)
    sub = close[np.ix_(idx, idx)]
    degree = sub.sum(axis=1)
    order = sorted(range(len(idx)), key=lambda k: (degree[k], idx[k]))
    accepted: list[int] = []
    for k in order:
        if all(not sub[k, a] for a in accepted):
            accepted.append(k)
    return sorted(idx[a] for a in accepted)


# --------------------------------------------------------------------------
# Window-width sweep
# --------------------------------------------------------------------------

def max_curve(times, loc_idx, close: np.ndarray, widths, episode_levels=(2, 3)):
    """Scan every width in ``widths`` over the same event list.

    ``times`` are seconds, non-decreasing; ``loc_idx`` are indices into
    ``close``. Returns one dict per width with the maximum number of
    well-separated locations, the maximum raw count, when the maximum occurs,
    and how many disjoint episodes reach each level in ``episode_levels``.

    The maximum is exact over ALL window positions, not only the aligned bins
    the SQL histogram uses: it is always attained by a window whose left edge
    sits on a detection, so scanning those positions covers every possibility.
    """
    times = np.asarray(times, float)
    loc_idx = np.asarray(loc_idx, int)
    n = len(times)
    out = []
    for width in widths:
        best = 0
        best_raw = 0
        best_t = None
        hits: dict[int, list[float]] = {k: [] for k in episode_levels}
        j = 0
        for i in range(n):
            limit = times[i] + width
            if j < i:
                j = i
            while j < n and times[j] < limit:
                j += 1
            members = set(loc_idx[i:j].tolist())
            if len(members) > best_raw:
                best_raw = len(members)
            counted = (len(members) if len(members) < 2
                       else len(independent_set(members, close)))
            if counted > best:
                best = counted
                best_t = times[i]
            for k in episode_levels:
                if counted >= k:
                    hits[k].append(times[i])
        episodes = {}
        for k, ts in hits.items():
            # Greedy thinning: one episode per non-overlapping window.
            count = 0
            last = -np.inf
            for t in ts:
                if t >= last + width:
                    count += 1
                    last = t
            episodes[k] = count
        out.append({
            "width_s": int(width),
            "max_counted": int(best),
            "max_raw": int(best_raw),
            "argmax_time_s": None if best_t is None else float(best_t),
            "episodes": episodes,
        })
    return out


def pair_min_width(times_by_loc: dict) -> dict:
    """Smallest observed time gap between detections at each pair of locations.

    ``times_by_loc`` maps a location key to a sorted array of times in seconds.
    Returns ``{(a, b): gap_seconds}`` with ``a < b``, computed by merging the
    two sorted series, so the cost is linear per pair.
    """
    keys = sorted(times_by_loc)
    out: dict[tuple, float] = {}
    for ia, a in enumerate(keys):
        ta = np.asarray(times_by_loc[a], float)
        if not ta.size:
            continue
        for b in keys[ia + 1:]:
            tb = np.asarray(times_by_loc[b], float)
            if not tb.size:
                continue
            pos = np.searchsorted(tb, ta)
            left = np.clip(pos - 1, 0, len(tb) - 1)
            right = np.clip(pos, 0, len(tb) - 1)
            out[(a, b)] = float(np.min(np.minimum(np.abs(ta - tb[left]),
                                                  np.abs(ta - tb[right]))))
    return out


# --------------------------------------------------------------------------
# Database access
# --------------------------------------------------------------------------

def _set_timeout(conn):
    """Cap every query of this request.

    ``SET LOCAL`` takes no bind parameters, hence the interpolation of an int
    constant. It lasts only for the surrounding transaction, so the pooled
    connection goes back unchanged.
    """
    conn.execute(text(f"SET LOCAL statement_timeout = {int(STATEMENT_TIMEOUT_MS)}"))


def resolve_species(conn, token):
    """Accept a species_id or a scientific name; return ``(id, scientific_name)``."""
    token = (str(token) if token is not None else "").strip()
    if not token:
        raise CooccurrenceError("Вид не вибрано.")
    if token.isdigit():
        row = conn.execute(text(
            "SELECT species_id, scientific_name FROM species WHERE species_id = :sid"
        ), {"sid": int(token)}).fetchone()
    else:
        row = conn.execute(text(
            "SELECT species_id, scientific_name FROM species "
            "WHERE lower(scientific_name) = lower(:name)"
        ), {"name": token}).fetchone()
    if not row:
        raise CooccurrenceError(f"Вид не знайдено: {token}")
    return int(row[0]), row[1]


LOCATIONS_SQL = """
    SELECT l.location_id,
           l.location_name,
           l.location_name_en,
           l.lat::double precision AS lat,
           l.lon::double precision AS lon
    FROM locations l
    WHERE l.lat IS NOT NULL AND l.lon IS NOT NULL
      AND {access}
      {biotopes}
      {locations}
    ORDER BY l.location_id
"""


def fetch_locations(conn, access_condition, access_params,
                    biotope_ids=None, location_ids=None, lang_code="uk"):
    """Locations passing the access rules and the page filters.

    ``access_condition`` / ``access_params`` come from the host's
    ``get_institution_filter``, so visibility and the institution picker are
    handled in exactly one place rather than duplicated here.

    The two optional predicates are spliced in only when the filter is set: an
    always-present clause would need a typed NULL, and ``text()`` cannot carry
    a ``:param::type`` cast (it reads the ``::`` as another parameter).
    """
    params = dict(access_params)
    biotopes_sql = ""
    locations_sql = ""
    if biotope_ids:
        params["biotope_ids"] = list(biotope_ids)
        biotopes_sql = ("AND EXISTS (SELECT 1 FROM location_biotopes lb "
                        "WHERE lb.location_id = l.location_id "
                        "AND lb.biotope_id = ANY(:biotope_ids))")
    if location_ids:
        params["location_ids"] = list(location_ids)
        locations_sql = "AND l.location_id = ANY(:location_ids)"
    sql = LOCATIONS_SQL.format(access=access_condition,
                               biotopes=biotopes_sql, locations=locations_sql)
    rows = conn.execute(text(sql), params).mappings().fetchall()
    out = []
    for r in rows:
        name = r["location_name"]
        if lang_code == "en" and r["location_name_en"]:
            name = r["location_name_en"]
        out.append({
            "location_id": int(r["location_id"]),
            "name": name,
            "lat": float(r["lat"]),
            "lon": float(r["lon"]),
        })
    return out


#: A season window is a (month, day) range that ignores the year, so the same
#: calendar stretch can be pooled across every year of the record. Compared as
#: ``month * 100 + day`` rather than day-of-year, because day-of-year shifts by
#: one after 29 February and would silently move the window in leap years.
#: The reconstructed detection time is read in UTC, like everything else here.
SEASON_EXPR = ("(extract(month from (({ts}) AT TIME ZONE 'UTC')) * 100"
               " + extract(day from (({ts}) AT TIME ZONE 'UTC')))")


#: The reconstructed detection timestamp, as written in both queries.
DET_TS_SQL = "r.datetime_start + (d.start_s * interval '1 second')"


def season_clause(ts_expr, season_from, season_to):
    """SQL keeping only detections inside the season window, or ``""``.

    A window that wraps the new year (``1201`` to ``0215``) is the union of two
    ranges, not an interval, so the two cases are written out separately.
    """
    if not season_from or not season_to:
        return ""
    expr = SEASON_EXPR.format(ts=ts_expr)
    if int(season_from) <= int(season_to):
        return f"AND {expr} BETWEEN :season_from AND :season_to"
    return f"AND ({expr} >= :season_from OR {expr} <= :season_to)"


#: Days in each month for the purpose of a season boundary. February gets 29,
#: so a "third decade of February" window includes 29 February in leap years
#: and simply matches nothing on that day in ordinary ones.
MONTH_LAST_DAY = (31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def season_decades():
    """The 36 ten-day periods of the year, as season-window boundaries.

    A decade (10-day third of a month) is the unit field seasons are named in:
    "April, first decade" rather than "1 April". Each entry carries both
    boundaries, so the "from" picker offers the first day of the decade and the
    "to" picker the last, and the two lists are the same 36 rows.
    """
    out = []
    for month in range(1, 13):
        last = MONTH_LAST_DAY[month - 1]
        for decade, (first_day, last_day) in enumerate(
                ((1, 10), (11, 20), (21, last)), start=1):
            out.append({
                "month": month,
                "decade": decade,
                "from_day": first_day,
                "to_day": last_day,
                "from_mmdd": month * 100 + first_day,
                "to_mmdd": month * 100 + last_day,
            })
    return out


def mmdd(value):
    """``MM * 100 + DD`` from a ``date``/``datetime``, an int, or ``MM-DD``."""
    if value in (None, "", 0):
        return None
    if isinstance(value, (dt.date, dt.datetime)):
        return value.month * 100 + value.day
    text_value = str(value).strip()
    if "-" in text_value:
        parts = text_value.split("-")
        month, day = int(parts[-2]), int(parts[-1])
    else:
        number = int(text_value)
        month, day = divmod(number, 100)
    if not (1 <= month <= 12 and 1 <= day <= 31):
        raise CooccurrenceError(f"Не розпізнано межу сезону: {value}")
    return month * 100 + day



#: Segments of one detection that this person could still verify: pending, and
#: without a vote of theirs already on file (a skip or an "unknown" counts as a
#: vote and hides the segment, matching the verification queue exactly). The
#: whole expression collapses to 0 when nobody is asking, so an anonymous run
#: pays nothing for it.
VERIFIABLE_SQL = """(
    SELECT count(*) FROM segments seg_v
    WHERE seg_v.detection_id = d.detection_id
      AND seg_v.status = 'pending'
      AND seg_v.id NOT IN (SELECT sv_v.segment_id FROM segment_verifications sv_v
                           WHERE sv_v.user_id = :verifier_user_id)
      AND {access}
)"""


def verifiable_expr(verifier_user_id, segment_access_sql):
    """The per-detection verifiable count, or a literal 0.

    ``segment_access_sql`` is the host's segment access baseline written against
    the alias ``seg_v``; it is passed in rather than rebuilt here so the page
    and the verification queue can never drift apart on who may see what.
    """
    if not verifier_user_id:
        return "0"
    return VERIFIABLE_SQL.format(access=segment_access_sql or "TRUE")


WINDOW_SQL = """
WITH det AS (
    SELECT r.location_id,
           date_bin(
               CAST(:width AS interval),
               r.datetime_start + (d.start_s * interval '1 second'),
               CAST(:origin AS timestamptz)
           ) AS window_start,
           dvm.verification_result                    AS consensus,
           COALESCE(seg.positive_verifications, 0)    AS positive_votes,
           {verifiable}                               AS verifiable
    FROM detections d
    JOIN recordings r ON r.recording_id = d.recording_id
    LEFT JOIN detection_verification_map dvm ON dvm.detection_id = d.detection_id
    LEFT JOIN segments seg ON seg.id = dvm.segment_id
    WHERE d.species_id = :species_id
      AND r.location_id = ANY(:loc_ids)
      AND r.datetime_start >= CAST(:start AS timestamptz) - CAST(:slack AS interval)
      AND r.datetime_start <  CAST(:end AS timestamptz)
      AND d.{conf} IS NOT NULL
      AND d.{conf} >= :min_conf
      {season}
)
SELECT window_start, location_id,
       count(*)::int                                        AS n_detections,
       COALESCE(max(positive_votes), 0)::int                AS positive_votes,
       count(*) FILTER (WHERE consensus = 1)::int           AS n_confirmed,
       count(*) FILTER (WHERE consensus = 0)::int           AS n_rejected,
       COALESCE(sum(verifiable), 0)::int                    AS n_verifiable
FROM det
WHERE window_start >= CAST(:start AS timestamptz)
  AND window_start <  CAST(:end AS timestamptz)
GROUP BY window_start, location_id
ORDER BY window_start, location_id
"""


def fetch_window_table(conn, species_id, start, end, width_s, origin, min_conf,
                       conf_column, loc_ids, season_from=None, season_to=None,
                       verifier_user_id=None, segment_access_sql=None,
                       segment_access_params=None):
    """Rows of ``(window_start, location_id, n_detections, positive_votes,
    n_confirmed, n_rejected, n_verifiable)``.

    All aggregation happens server-side, so only the small (window, location)
    table travels to Python. ``origin`` anchors the bins: shifting it by less
    than the width is how overlapping windows are produced.
    """
    if conf_column not in CONF_COLUMNS:
        raise CooccurrenceError(f"Невідома колонка confidence: {conf_column}")
    sql = WINDOW_SQL.format(
        conf=conf_column,
        season=season_clause(DET_TS_SQL, season_from, season_to),
        verifiable=verifiable_expr(verifier_user_id, segment_access_sql),
    )
    params = {
        "species_id": species_id,
        "start": start,
        "end": end,
        "width": f"{int(width_s)} seconds",
        "origin": origin,
        "min_conf": float(min_conf),
        "loc_ids": list(loc_ids),
        "slack": RECORDING_SLACK,
        "season_from": season_from,
        "season_to": season_to,
    }
    if verifier_user_id:
        params["verifier_user_id"] = verifier_user_id
        params.update(segment_access_params or {})
    rows = conn.execute(text(sql), params).fetchall()
    return [(r[0], int(r[1]), int(r[2]), int(r[3]), int(r[4]), int(r[5]),
             int(r[6])) for r in rows]


EVENTS_SQL = """
SELECT r.location_id,
       r.datetime_start + (d.start_s * interval '1 second') AS det_ts
FROM detections d
JOIN recordings r ON r.recording_id = d.recording_id
WHERE d.species_id = :species_id
  AND r.location_id = ANY(:loc_ids)
  AND r.datetime_start >= CAST(:start AS timestamptz) - CAST(:slack AS interval)
  AND r.datetime_start <  CAST(:end AS timestamptz)
  AND d.{conf} IS NOT NULL
  AND d.{conf} >= :min_conf
  AND r.datetime_start + (d.start_s * interval '1 second') >= CAST(:start AS timestamptz)
  AND r.datetime_start + (d.start_s * interval '1 second') <  CAST(:end AS timestamptz)
  {season}
ORDER BY det_ts, r.location_id
"""


def _events_params(species_id, start, end, min_conf, loc_ids,
                   season_from=None, season_to=None):
    return {
        "species_id": species_id,
        "start": start,
        "end": end,
        "min_conf": float(min_conf),
        "loc_ids": list(loc_ids),
        "slack": RECORDING_SLACK,
        "season_from": season_from,
        "season_to": season_to,
    }


def count_detection_events(conn, species_id, start, end, min_conf, conf_column,
                           loc_ids, season_from=None, season_to=None):
    if conf_column not in CONF_COLUMNS:
        raise CooccurrenceError(f"Невідома колонка confidence: {conf_column}")
    inner = EVENTS_SQL.format(
        conf=conf_column,
        season=season_clause(DET_TS_SQL, season_from, season_to),
    )
    return int(conn.execute(
        text("SELECT count(*) FROM (" + inner + ") q"),
        _events_params(species_id, start, end, min_conf, loc_ids,
                       season_from, season_to)).scalar() or 0)


def fetch_detection_events(conn, species_id, start, end, min_conf, conf_column,
                           loc_ids, season_from=None, season_to=None):
    """Individual detections as ``(location_id, timestamp)``, time-ordered.

    The sweep needs raw event times, because binning in SQL fixes one width per
    query. Fetching them once and scanning in Python covers every width in a
    single round trip.
    """
    if conf_column not in CONF_COLUMNS:
        raise CooccurrenceError(f"Невідома колонка confidence: {conf_column}")
    sql = EVENTS_SQL.format(
        conf=conf_column,
        season=season_clause(DET_TS_SQL, season_from, season_to),
    )
    rows = conn.execute(text(sql),
                        _events_params(species_id, start, end, min_conf, loc_ids,
                                       season_from, season_to)).fetchall()
    return [(int(r[0]), r[1]) for r in rows]


def period_bounds(conn, access_condition, access_params):
    """Earliest and latest recording start visible to this person, as dates."""
    row = conn.execute(text(
        "SELECT min(r.datetime_start)::date, max(r.datetime_start)::date "
        "FROM recordings r JOIN locations l ON l.location_id = r.location_id "
        f"WHERE {access_condition}"
    ), dict(access_params)).fetchone()
    if not row or row[0] is None:
        return None, None
    return row[0].isoformat(), row[1].isoformat()


#: How a (window, location) is marked once people have listened to it. A
#: detection a person confirmed is a different kind of evidence from a
#: classifier score, so it has to be visible at a glance rather than hidden in
#: a popup: such a window is the one worth citing.
#:   2 — consensus, or two or more positive votes
#:   1 — exactly one positive vote, no consensus yet
#:   0 — nobody has listened
#:  -1 — someone listened and the consensus was "not this species"
#: Counting is deliberately NOT changed by this: a rejected detection still
#: occupies its window, it is only labelled. Dropping rejected detections from
#: the bound is a separate decision, noted in the README.
def verification_level(positive_votes, n_confirmed, n_rejected):
    if n_confirmed > 0 or positive_votes >= 2:
        return 2
    if positive_votes == 1:
        return 1
    if n_rejected > 0:
        return -1
    return 0


# --------------------------------------------------------------------------
# Counting
# --------------------------------------------------------------------------

def count_windows(per_window, xy, loc2cluster, min_distance_m, cluster_method):
    """Decide, for every window, which locations count towards the lower bound.

    ``greedy`` enforces the spacing rule INSIDE each window: a largest-first
    subset of the detecting locations that is pairwise at least
    ``min_distance_m`` apart. That is the operation the argument needs, and no
    location is discarded a priori.

    ``complete`` and ``single`` instead use the globally pre-computed clusters
    in ``loc2cluster`` and keep one representative (most detections) per
    cluster, so the counted set is a property of the network rather than of the
    window.

    Returns ``(selected, summary)``.
    """
    selected: dict = {}
    summary = []
    for win in sorted(per_window):
        by_loc = per_window[win]
        if cluster_method == "greedy" and min_distance_m > 0:
            order = sorted(by_loc, key=lambda l: (-by_loc[l], l))
            keep = greedy_spaced_subset(order, xy, min_distance_m)
        else:
            best: dict[int, int] = {}
            for lid, n in by_loc.items():
                c = loc2cluster[lid]
                if c not in best or (n, -lid) > (by_loc[best[c]], -best[c]):
                    best[c] = lid
            keep = sorted(best.values())
        selected[win] = sorted(keep)
        summary.append({
            "window_start": win,
            "n_counted": len(keep),
            "n_locations_raw": len(by_loc),
            "n_detections": sum(by_loc.values()),
        })
    return selected, summary


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def _cluster(locs, xs, ys, min_distance_m, cluster_method):
    """Global acoustic clusters, or the identity mapping for ``greedy``."""
    loc_ids = [r["location_id"] for r in locs]
    if cluster_method == "greedy" or min_distance_m <= 0:
        return {lid: i for i, lid in enumerate(loc_ids)}, len(loc_ids)
    fn = (cluster_locations_complete if cluster_method == "complete"
          else cluster_locations_single)
    labels, loc2cluster = fn(loc_ids, xs, ys, min_distance_m)
    return loc2cluster, len(set(labels.tolist()))


def run(conn, *, species, start, end, window_s, step_s, min_distance_m,
        cluster_method, min_conf, conf_column, access_condition, access_params,
        biotope_ids=None, location_ids=None, min_locations=1,
        season_from=None, season_to=None,
        verifier_user_id=None, segment_access_sql=None,
        segment_access_params=None,
        detail_windows=DETAIL_WINDOWS, lang_code="uk"):
    """The main pass: window table, spacing rule, histogram, per-window detail.

    ``detail_windows`` limits how many windows come back with their full point
    list (the map needs one window at a time). ``None`` returns every kept
    window, which is what the CSV export wants.
    """
    _set_timeout(conn)
    species_id, species_name = resolve_species(conn, species)

    if end <= start:
        raise CooccurrenceError("Кінець періоду має бути після початку.")
    window_s = int(window_s)
    step_s = int(step_s or window_s)
    if window_s <= 0 or step_s <= 0:
        raise CooccurrenceError("Ширина і крок вікна мають бути додатними.")
    if step_s > window_s:
        raise CooccurrenceError("Крок вікна не може перевищувати його ширину.")
    n_offsets = max(1, window_s // step_s) if step_s < window_s else 1
    if n_offsets > MAX_DB_PASSES:
        raise CooccurrenceError(
            f"Такий крок вимагає {n_offsets} проходів по базі "
            f"(максимум {MAX_DB_PASSES}). Збільште крок."
        )
    if cluster_method not in CLUSTER_METHODS:
        raise CooccurrenceError(f"Невідомий метод розведення: {cluster_method}")
    min_distance_m = float(min_distance_m)

    locs = fetch_locations(conn, access_condition, access_params,
                           biotope_ids, location_ids, lang_code)
    if not locs:
        raise CooccurrenceError("Жодна локація не проходить фільтри.")

    loc_ids = [r["location_id"] for r in locs]
    lats = np.array([r["lat"] for r in locs], float)
    lons = np.array([r["lon"] for r in locs], float)
    crs = pick_utm_crs(lons, lats)
    xs, ys = project(lons, lats, crs)
    xy = {lid: (float(x), float(y)) for lid, x, y in zip(loc_ids, xs, ys)}
    loc2cluster, n_clusters = _cluster(locs, xs, ys, min_distance_m, cluster_method)

    # window_start -> {location_id: n_detections}, and the verification state
    # of the same cell kept beside it so the counting code stays about counting.
    per_window: dict = defaultdict(lambda: defaultdict(int))
    verif: dict = defaultdict(dict)
    total_detections = 0
    for k in range(n_offsets):
        origin = start + dt.timedelta(seconds=k * step_s)
        for (win, lid, n_det, votes, n_conf, n_rej, n_verifiable) in                 fetch_window_table(
                conn, species_id, start, end, window_s, origin,
                min_conf, conf_column, loc_ids, season_from, season_to,
                verifier_user_id, segment_access_sql, segment_access_params):
            per_window[win][lid] += n_det
            total_detections += n_det
            cell = verif[win].setdefault(lid, {"votes": 0, "confirmed": 0,
                                               "rejected": 0, "verifiable": 0})
            cell["votes"] = max(cell["votes"], votes)
            cell["confirmed"] += n_conf
            cell["rejected"] += n_rej
            # A maximum, not a sum: overlapping passes report the same cell
            # more than once, and the count belongs to the cell.
            cell["verifiable"] = max(cell["verifiable"], n_verifiable)

    result = {
        "species": {"id": species_id, "name": species_name},
        "params": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "window_s": window_s,
            "step_s": step_s,
            "min_distance_m": min_distance_m,
            "cluster_method": cluster_method,
            "min_conf": float(min_conf),
            "conf_column": conf_column,
            "min_locations": int(min_locations),
            "season_from": season_from,
            "season_to": season_to,
            "db_passes": n_offsets,
            "crs": crs.to_string(),
        },
        "locations": [
            {"id": r["location_id"], "name": r["name"], "lat": r["lat"], "lon": r["lon"]}
            for r in locs
        ],
        "n_locations": len(locs),
        "n_clusters": n_clusters,
        "n_detections": total_detections,
    }

    if not per_window:
        result.update({
            "empty": True, "histogram": [], "windows": [], "window_detail": {},
            "peak_window": None, "max_counted": 0, "max_raw": 0,
            "n_windows": 0, "n_windows_kept": 0, "n_windows_ge2": 0,
            "n_windows_verified": 0, "n_windows_listed": 0,
            "max_positive_votes": 0,
            "table_min_counted": TABLE_MIN_COUNTED, "windows_singles_only": False,
        })
        return result

    selected, summary = count_windows(per_window, xy, loc2cluster,
                                      min_distance_m, cluster_method)
    kept = [s for s in summary if s["n_counted"] >= int(min_locations)]
    counts = [s["n_counted"] for s in kept]

    hist: dict[int, int] = defaultdict(int)
    for c in counts:
        hist[c] += 1

    # A counted location that a person confirmed is what makes a window
    # citable, so verified windows rank above merely large ones. The lower
    # bound itself is unchanged; only the order of the list is.
    for s_row in summary:
        win = s_row["window_start"]
        chosen = set(selected[win])
        cells = verif.get(win, {})
        levels = [verification_level(c["votes"], c["confirmed"], c["rejected"])
                  for lid, c in cells.items() if lid in chosen]
        s_row["n_verified"] = sum(1 for lv in levels if lv > 0)
        s_row["max_level"] = max(levels) if levels else 0
        s_row["max_votes"] = max((c["votes"] for lid, c in cells.items()
                                  if lid in chosen), default=0)

    # The table is about co-occurrence, so single-location windows are left out
    # of it (they stay in the histogram, the KPIs and the CSV export).
    listed = [s for s in kept if s["n_counted"] >= TABLE_MIN_COUNTED]
    singles_only = not listed
    if singles_only:
        listed = kept

    by_verified = sorted(listed, key=lambda s: (-s["n_verified"], -s["max_level"],
                                                -s["max_votes"], -s["n_counted"],
                                                -s["n_detections"],
                                                s["window_start"]))
    by_count = sorted(listed, key=lambda s: (-s["n_counted"], -s["n_detections"],
                                             s["window_start"]))
    if detail_windows is None:
        ranked = sorted(kept, key=lambda s: (-s["n_verified"], -s["max_level"],
                                             -s["max_votes"], -s["n_counted"],
                                             -s["n_detections"],
                                             s["window_start"]))
    else:
        # Two things have to survive the cut: the windows a person confirmed
        # (the citable ones) and the windows with the most simultaneous
        # locations (the headline). Ranking by verification alone would push a
        # three-location peak off a list full of single-location verified
        # windows, so the slice is the union of both heads, presented
        # verified-first. The table is sortable, so either view is one click.
        limit = int(detail_windows)
        keep_keys = {s["window_start"] for s in by_verified[:limit]}
        keep_keys.update(s["window_start"] for s in by_count[:max(10, limit // 2)])
        ranked = [s for s in by_verified if s["window_start"] in keep_keys]
    detail_source = ranked
    ll = {r["location_id"]: r for r in locs}
    window_detail = {}
    for s in detail_source:
        win = s["window_start"]
        chosen = set(selected[win])
        window_detail[win.isoformat()] = [
            {
                "location_id": lid,
                "name": ll[lid]["name"],
                "lat": ll[lid]["lat"],
                "lon": ll[lid]["lon"],
                "x": round(xy[lid][0], 1),
                "y": round(xy[lid][1], 1),
                "cluster_id": loc2cluster[lid],
                "counted": lid in chosen,
                "n_detections": n_det,
                "positive_votes": verif.get(win, {}).get(lid, {}).get("votes", 0),
                "n_confirmed": verif.get(win, {}).get(lid, {}).get("confirmed", 0),
                "n_rejected": verif.get(win, {}).get(lid, {}).get("rejected", 0),
                "n_verifiable": verif.get(win, {}).get(lid, {}).get("verifiable", 0),
                "verification_level": verification_level(
                    verif.get(win, {}).get(lid, {}).get("votes", 0),
                    verif.get(win, {}).get(lid, {}).get("confirmed", 0),
                    verif.get(win, {}).get(lid, {}).get("rejected", 0)),
            }
            for lid, n_det in sorted(per_window[win].items(),
                                     key=lambda kv: (-kv[1], kv[0]))
        ]

    result.update({
        "empty": False,
        "histogram": [{"n": k, "windows": hist[k]} for k in sorted(hist)],
        "windows": [
            {"start": s["window_start"].isoformat(),
             "n_counted": s["n_counted"],
             "n_locations_raw": s["n_locations_raw"],
             "n_detections": s["n_detections"],
             "n_verified": s["n_verified"],
             "max_level": s["max_level"],
             "max_votes": s["max_votes"]}
            for s in ranked
        ],
        "window_detail": window_detail,
        "peak_window": by_count[0]["window_start"].isoformat() if by_count else None,
        "table_min_counted": TABLE_MIN_COUNTED,
        "windows_singles_only": singles_only,
        "n_windows_listed": len(listed),
        "max_counted": max(counts) if counts else 0,
        "max_raw": max((s["n_locations_raw"] for s in summary), default=0),
        "n_windows": len(summary),
        "n_windows_kept": len(kept),
        "n_windows_ge2": sum(1 for c in counts if c >= 2),
        "n_windows_verified": sum(1 for s in kept if s["n_verified"] > 0),
        # Most positive human votes on any single (window, location) in this
        # result. The map ramps its greens against this, so the scale is fixed
        # for the whole run rather than per window: a shade then means the same
        # thing while flipping between windows, and the darkest green in view
        # really is the best-verified point in the selection.
        "max_positive_votes": max(
            (c["votes"] for cells in verif.values() for c in cells.values()),
            default=0),
    })
    # The full per-window summary is only for the CSV export; it can be large.
    if detail_windows is None:
        result["all_windows"] = summary
        result["all_selected"] = selected
        result["all_per_window"] = {w: dict(v) for w, v in per_window.items()}
        result["all_verif"] = {w: dict(v) for w, v in verif.items()}
        result["_xy"] = xy
        result["_loc2cluster"] = loc2cluster
    return result


def run_sweep(conn, *, species, start, end, min_distance_m, min_conf, conf_column,
              access_condition, access_params, biotope_ids=None, location_ids=None,
              season_from=None, season_to=None,
              sweep_min_s=DEFAULT_SWEEP_MIN_S, sweep_max_s=DEFAULT_SWEEP_MAX_S,
              sweep_step_s=DEFAULT_SWEEP_STEP_S, lang_code="uk"):
    """How much of the answer is the window width, and which pairs co-detect.

    Both come from one fetch of raw detection times: binning in SQL fixes a
    single width per query, so sweeping dozens of widths that way would mean
    dozens of round trips.
    """
    _set_timeout(conn)
    species_id, species_name = resolve_species(conn, species)
    if end <= start:
        raise CooccurrenceError("Кінець періоду має бути після початку.")

    lo = max(1, int(sweep_min_s))
    hi = int(sweep_max_s)
    stp = max(1, int(sweep_step_s))
    if hi < lo:
        raise CooccurrenceError("Максимальна ширина менша за мінімальну.")
    widths = list(range(lo, hi + 1, stp))
    if len(widths) > SWEEP_MAX_WIDTHS:
        raise CooccurrenceError(
            f"Розгортка з {len(widths)} значень ширини (максимум "
            f"{SWEEP_MAX_WIDTHS}). Збільште крок або зменште діапазон."
        )

    locs = fetch_locations(conn, access_condition, access_params,
                           biotope_ids, location_ids, lang_code)
    if not locs:
        raise CooccurrenceError("Жодна локація не проходить фільтри.")
    loc_ids = [r["location_id"] for r in locs]
    lats = np.array([r["lat"] for r in locs], float)
    lons = np.array([r["lon"] for r in locs], float)
    crs = pick_utm_crs(lons, lats)
    xs, ys = project(lons, lats, crs)
    xy = {lid: (float(x), float(y)) for lid, x, y in zip(loc_ids, xs, ys)}

    n_events = count_detection_events(conn, species_id, start, end,
                                      min_conf, conf_column, loc_ids,
                                      season_from, season_to)
    if n_events == 0:
        return {"empty": True, "n_events": 0, "species": {"id": species_id,
                                                          "name": species_name}}
    if n_events > SWEEP_MAX_EVENTS:
        raise CooccurrenceError(
            f"Розгортка над {n_events} детекціями надто дорога (ліміт "
            f"{SWEEP_MAX_EVENTS}). Звужте період, підніміть поріг confidence або "
            f"обмежте локації."
        )

    events = fetch_detection_events(conn, species_id, start, end,
                                    min_conf, conf_column, loc_ids,
                                    season_from, season_to)
    t0 = min(ts for _lid, ts in events)
    idx_of = {lid: i for i, lid in enumerate(loc_ids)}
    times = [(ts - t0).total_seconds() for _lid, ts in events]
    lidx = [idx_of[lid] for lid, _ts in events]

    close = close_matrix(xs, ys, float(min_distance_m))
    rows = max_curve(times, lidx, close, widths, episode_levels=(2, 3))

    times_by_loc: dict[int, list[float]] = defaultdict(list)
    for t, li in zip(times, lidx):
        times_by_loc[li].append(t)
    gaps = pair_min_width({k: sorted(v) for k, v in times_by_loc.items()})

    pairs = []
    for (a, b), gap in sorted(gaps.items(), key=lambda kv: kv[1]):
        if close[a, b] or gap > hi:
            continue
        la, lb = loc_ids[a], loc_ids[b]
        pairs.append({
            "a": la, "b": lb,
            "name_a": locs[a]["name"], "name_b": locs[b]["name"],
            "lat_a": locs[a]["lat"], "lon_a": locs[a]["lon"],
            "lat_b": locs[b]["lat"], "lon_b": locs[b]["lon"],
            "distance_m": round(math.dist(xy[la], xy[lb])),
            "min_window_s": round(gap, 1),
        })

    # Where the curve steps up: the interesting part of the parameter space.
    first_reach = []
    seen = 0
    for r in rows:
        if r["max_counted"] > seen:
            seen = r["max_counted"]
            at = (None if r["argmax_time_s"] is None
                  else (t0 + dt.timedelta(seconds=r["argmax_time_s"])).isoformat())
            first_reach.append({"level": seen, "width_s": r["width_s"], "first_at": at})

    return {
        "empty": False,
        "species": {"id": species_id, "name": species_name},
        "n_events": n_events,
        "min_distance_m": float(min_distance_m),
        "curve": [
            {"width_s": r["width_s"], "max_counted": r["max_counted"],
             "max_raw": r["max_raw"],
             "episodes_ge2": r["episodes"].get(2, 0),
             "episodes_ge3": r["episodes"].get(3, 0),
             "argmax_time": (None if r["argmax_time_s"] is None
                             else (t0 + dt.timedelta(seconds=r["argmax_time_s"])
                                   ).isoformat())}
            for r in rows
        ],
        "first_reach": first_reach,
        "pairs": pairs,
        "locations": [
            {"id": r["location_id"], "name": r["name"], "lat": r["lat"], "lon": r["lon"]}
            for r in locs
        ],
        "crs": crs.to_string(),
    }
