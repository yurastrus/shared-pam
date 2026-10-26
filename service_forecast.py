"""
Recorder service forecast for the PAM manage-locations page.

Pure logic, no DB access: the routes read service visits and recordings and pass
them here as plain values. Kept apart from routes.py so it can be tested
directly (same pattern as gap_metrics.py).

What the forecast answers: how many days each deployed recorder has left before
its batteries or its SD card run out, and so by which date someone should visit
it before data start to be lost.

Three rules that the old inline code got wrong:

* "Batteries kept" / "card kept" do not reset the clock. The remaining resource
  is carried over from the previous visit minus what was used since then.
* Battery capacity is learned from history. A run that stopped recording well
  before the next visit shows how many hours that battery type really gave.
  The rated value from `battery_types` is only the fallback.
* The SD data rate is learned from history too. Every run that wrote H hours to
  a card of C GB proves that rate <= C / H (the card was not overfilled), so the
  smallest such bound per location is a safe estimate for that recorder.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterable, Optional

# ── reference ids (visit_purposes, battery_types, sd_card_status) ───────────
PURPOSE_ROUTINE = 1
PURPOSE_INSTALL = 2
PURPOSE_REMOVED = 3
PURPOSE_OTHER = 4
BATTERY_KEPT_TYPE_ID = 7      # "Залишено наявні"
SD_KEPT_STATUS_ID = 1         # "Залишено наявну" (capacity_gb IS NULL)

# ── thresholds ──────────────────────────────────────────────────────────────
WARNING_DAYS = 14
CRITICAL_DAYS = 3
INACTIVE_DAYS = 200           # no data and no visit for this long → inactive
DEFAULT_DATA_RATE_MB_PER_HOUR = 290.0
MIN_DATA_RATE_MB_PER_HOUR = 30.0   # guard against a mislabelled card
STOP_GAP_DAYS = 3             # last recording this far before the next visit = run ended early
STOPPED_FACT_DAYS = 7         # current deployment silent this long + forecast exhausted = stopped
MIN_CALIBRATION_RUNS = 3
SD_LIMITED_SHARE = 0.9        # run filled >= 90 % of the card → card, not battery, stopped it

# ok / warning / critical: forecast says the resource lasts; overdue: the forecast
# says it already ran out; stopped: this deployment's data confirm it went silent.
STATUSES = ('ok', 'warning', 'critical', 'overdue', 'stopped',
            'removed', 'inactive', 'no_log', 'unknown')


@dataclass
class Visit:
    """One service_visits row plus what was recorded until the next visit."""
    visit_datetime: datetime
    purpose_id: int
    hours_per_day: Optional[int]
    battery_type_id: Optional[int] = None
    battery_rated_hours: Optional[float] = None   # battery_types.estimated_recording_hours
    card_capacity_gb: Optional[float] = None      # None = card kept
    is_operational: Optional[bool] = None
    last_recording: Optional[datetime] = None     # latest recording before the next visit
    recorded_hours: float = 0.0                   # sum of recording durations in that interval

    @property
    def day(self) -> date:
        return self.visit_datetime.date()

    @property
    def fresh_battery(self) -> bool:
        return self.battery_type_id is not None and self.battery_type_id != BATTERY_KEPT_TYPE_ID

    @property
    def fresh_card(self) -> bool:
        return self.card_capacity_gb is not None and self.card_capacity_gb > 0


@dataclass
class Calibration:
    """Values learned from the whole service history."""
    battery_hours: dict = field(default_factory=dict)   # type_id -> (hours, n_runs)
    location_rate: dict = field(default_factory=dict)   # location_id -> MB/h
    default_rate: float = DEFAULT_DATA_RATE_MB_PER_HOUR

    def battery_capacity(self, visit: Visit):
        """(hours, source, n_runs) for a fresh battery of this visit's type."""
        learned = self.battery_hours.get(visit.battery_type_id)
        if learned:
            return learned[0], 'history', learned[1]
        if visit.battery_rated_hours:
            return float(visit.battery_rated_hours), 'rated', 0
        return None, None, 0

    def rate(self, location_id) -> float:
        return self.location_rate.get(location_id, self.default_rate)


def _daily_gb(hours_per_day, rate_mb_h):
    if not hours_per_day or not rate_mb_h:
        return None
    return hours_per_day * rate_mb_h / 1024.0


def _runs(visits: list[Visit]):
    """(visit, next_visit) pairs of each deployment interval that has an end."""
    for cur, nxt in zip(visits, visits[1:]):
        if cur.purpose_id != PURPOSE_REMOVED:
            yield cur, nxt


def calibrate(visits_by_location: dict) -> Calibration:
    """Learn data rates and battery capacities from completed runs.

    `visits_by_location` maps location_id -> list[Visit] sorted by time.
    """
    cal = Calibration()

    # 1. Data rate: each run that wrote to a fresh card bounds the rate from above.
    for loc_id, visits in visits_by_location.items():
        bounds = [v.card_capacity_gb * 1024.0 / v.recorded_hours
                  for v, _ in _runs(visits)
                  if v.fresh_card and v.recorded_hours > 0]
        if bounds:
            cal.location_rate[loc_id] = max(MIN_DATA_RATE_MB_PER_HOUR,
                                            min(DEFAULT_DATA_RATE_MB_PER_HOUR, min(bounds)))
    if len(cal.location_rate) >= MIN_CALIBRATION_RUNS:
        # Unknown recorder: the median bound is still an upper bound for most
        # recorders, so the SD forecast errs on the early side.
        cal.default_rate = statistics.median(cal.location_rate.values())

    # 2. Battery capacity: runs that stopped early and did not fill the card.
    samples: dict = {}
    for loc_id, visits in visits_by_location.items():
        rate = cal.rate(loc_id)
        for v, nxt in _runs(visits):
            if not (v.fresh_battery and v.hours_per_day and v.last_recording):
                continue
            if (nxt.day - v.last_recording.date()).days < STOP_GAP_DAYS:
                continue        # still recording at the next visit: battery did not run out
            if v.fresh_card and v.recorded_hours * rate / 1024.0 >= SD_LIMITED_SHARE * v.card_capacity_gb:
                continue        # the card filled up first
            days = (v.last_recording.date() - v.day).days
            if days > 0:
                samples.setdefault(v.battery_type_id, []).append(days * v.hours_per_day)
    for type_id, values in samples.items():
        if len(values) >= MIN_CALIBRATION_RUNS:
            cal.battery_hours[type_id] = (float(statistics.median(values)), len(values))
    return cal


@dataclass
class _State:
    """Resources left right after a visit."""
    visit: Visit
    battery_hours_left: Optional[float]
    battery_source: Optional[str]
    battery_runs: int
    card_free_gb: Optional[float]


def _replay(visits: list[Visit], cal: Calibration, location_id) -> Optional[_State]:
    """Walk the visits in order, carrying over kept batteries and cards."""
    state = None
    for v in visits:
        if v.purpose_id == PURPOSE_REMOVED:
            state = None
            continue
        elapsed = (v.day - state.visit.day).days if state else 0
        prev_hours = state.visit.hours_per_day if state else None

        if v.fresh_battery:
            left, source, runs = cal.battery_capacity(v)
        elif state and state.battery_hours_left is not None and prev_hours:
            left = max(0.0, state.battery_hours_left - elapsed * prev_hours)
            source, runs = 'kept', state.battery_runs
        else:
            left, source, runs = None, None, 0

        if v.fresh_card:
            free = float(v.card_capacity_gb)
        elif state and state.card_free_gb is not None:
            used = _daily_gb(prev_hours, cal.rate(location_id)) or 0.0
            free = max(0.0, state.card_free_gb - elapsed * used)
        else:
            free = None

        state = _State(v, left, source, runs, free)
    return state


def _grade(days_left):
    if days_left is None:
        return None
    if days_left < 0:
        return 'overdue'
    if days_left <= CRITICAL_DAYS:
        return 'critical'
    if days_left <= WARNING_DAYS:
        return 'warning'
    return 'ok'


def forecast_location(location_id, visits: list[Visit], last_recording: Optional[datetime],
                      cal: Calibration, today: date) -> dict:
    """Service state of one location on `today`.

    Returns a dict with: status, reason (code), days_since_visit,
    battery_days_left, sd_card_days_left, limiting ('battery'|'sd'|None),
    visit_by (date|None), lost_days, last_recording (date of the latest
    recording in the current deployment), battery_source, battery_runs,
    battery_capacity_hours, hours_per_day, data_rate_mb_h.
    """
    out = dict(status='unknown', reason='no_visits', days_since_visit=None,
               battery_days_left=None, sd_card_days_left=None, limiting=None,
               visit_by=None, lost_days=None, last_recording=None,
               battery_source=None, battery_runs=0, battery_capacity_hours=None,
               hours_per_day=None, data_rate_mb_h=round(cal.rate(location_id)))
    last_rec_day = last_recording.date() if last_recording else None

    if not visits:
        if last_rec_day and (today - last_rec_day).days <= INACTIVE_DAYS:
            out.update(status='no_log', reason='no_log')
        else:
            out.update(status='inactive', reason='no_activity')
        return out

    last = visits[-1]
    out['days_since_visit'] = (today - last.day).days
    if last.purpose_id == PURPOSE_REMOVED:
        out.update(status='removed', reason='removed')
        return out

    activity = max(d for d in (last_rec_day, last.day) if d is not None)
    if (today - activity).days > INACTIVE_DAYS:
        out.update(status='inactive', reason='stale')
        return out

    state = _replay(visits, cal, location_id)
    days_since = out['days_since_visit']
    hours = last.hours_per_day
    out['hours_per_day'] = hours
    out['last_recording'] = last.last_recording.date() if last.last_recording else None

    battery_left = None
    if state.battery_hours_left is not None and hours:
        battery_left = state.battery_hours_left / hours - days_since
        out['battery_source'] = state.battery_source
        out['battery_runs'] = state.battery_runs
        if state.battery_source != 'kept':
            out['battery_capacity_hours'] = round(state.battery_hours_left)
    sd_left = None
    daily = _daily_gb(hours, cal.rate(location_id))
    if state.card_free_gb is not None and daily:
        sd_left = state.card_free_gb / daily - days_since

    out['battery_days_left'] = round(battery_left) if battery_left is not None else None
    out['sd_card_days_left'] = round(sd_left) if sd_left is not None else None

    known = [(d, k) for d, k in ((battery_left, 'battery'), (sd_left, 'sd')) if d is not None]
    if not known:
        out.update(status='unknown', reason='no_forecast')
        return out

    left, limiting = min(known)
    out['limiting'] = limiting
    exhausted_after = days_since + left
    out['visit_by'] = date.fromordinal(last.day.toordinal() + max(0, int(exhausted_after)))
    out.update(status=_grade(left), reason=limiting)
    if left < 0:
        out['lost_days'] = int(-left)

    # Fact beats forecast: this deployment's data are in and went silent.
    rec = out['last_recording']
    if rec and left <= 0 and (today - rec).days >= STOPPED_FACT_DAYS:
        out.update(status='stopped', reason='stopped', lost_days=(today - rec).days)
    return out


def visits_from_rows(rows: Iterable) -> dict:
    """Group DB rows (see routes.SERVICE_VISITS_SQL) into location_id -> [Visit]."""
    grouped: dict = {}
    for r in rows:
        grouped.setdefault(r.location_id, []).append(Visit(
            visit_datetime=r.visit_datetime,
            purpose_id=r.visit_purpose_id,
            hours_per_day=r.recording_hours_per_day,
            battery_type_id=r.battery_type_id,
            battery_rated_hours=(float(r.estimated_recording_hours)
                                 if r.estimated_recording_hours else None),
            card_capacity_gb=float(r.capacity_gb) if r.capacity_gb else None,
            is_operational=r.is_operational,
            last_recording=r.last_recording,
            recorded_hours=float(r.recorded_hours or 0),
        ))
    for visits in grouped.values():
        visits.sort(key=lambda v: v.visit_datetime)
    return grouped
