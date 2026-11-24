"""Decide, per archived period, what a merge of site S into site T has to do.

Pure logic, no database access. Inputs are what inventory.py reads:
visits per local day from the raw log tables, and the archived periods with
their nb_visits. Output is a Plan.

Rules (S = source, T = target):

* A day for which S still has raw logs: the raw logs move to T, the day is
  invalidated for T and Matomo re-archives it from the combined logs.
* A day for which S has only an archive (raw logs purged, or history imported
  from Google Analytics) and T has no data: the archive moves to T unchanged.
* A day for which S has only an archive and T has data as well: conflict.
  Two archives cannot be added up without the raw data behind them.
* A week, month or year archive of S moves to T if neither T nor the raw logs
  of S have data inside it. Otherwise T's archive of that period is
  invalidated and Matomo rebuilds it from the day archives. That is only
  correct if S's data in that period is fully present at day level; if S's
  period archive counts more visits than its days, it is a conflict.
* Range archives never move; T's ranges that overlap a changed day are deleted
  and Matomo recomputes them on demand.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

DAY, WEEK, MONTH, YEAR, RANGE = 1, 2, 3, 4, 5
PERIOD_NAMES = {DAY: "day", WEEK: "week", MONTH: "month", YEAR: "year", RANGE: "range"}


@dataclass(frozen=True, order=True)
class Period:
    period: int
    date1: dt.date
    date2: dt.date

    def days(self):
        d = self.date1
        while d <= self.date2:
            yield d
            d += dt.timedelta(days=1)

    def __str__(self) -> str:
        if self.period == DAY:
            return f"day {self.date1}"
        return f"{PERIOD_NAMES[self.period]} {self.date1}..{self.date2}"


@dataclass
class SiteData:
    """What one site has: raw visits per local day and archived periods."""

    raw_visits: dict[dt.date, int] = field(default_factory=dict)
    # nb_visits of the valid all-visits archive of each period (0 if the
    # archive exists but has no nb_visits record).
    archives: dict[Period, int] = field(default_factory=dict)

    def day_visits(self, d: dt.date) -> int:
        """Visits of a day as far as Matomo knows them (raw logs win)."""
        if d in self.raw_visits:
            return self.raw_visits[d]
        return self.archives.get(Period(DAY, d, d), 0)

    def has_data(self, d: dt.date) -> bool:
        return self.day_visits(d) > 0


@dataclass
class Conflict:
    period: Period
    reason: str


@dataclass
class Plan:
    move_raw_days: list[dt.date] = field(default_factory=list)
    move_archives: list[Period] = field(default_factory=list)
    # Target periods to invalidate, as (period name, date inside the period).
    invalidate: list[tuple[str, dt.date]] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    keep_target: list[Period] = field(default_factory=list)  # resolved conflicts
    # Archive-only days that will not be rebuilt but feed rebuilt parents.
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.conflicts


def _start(name: str, d: dt.date) -> dt.date:
    """First day of the Matomo period containing d (weeks start on Monday)."""
    if name == "week":
        return d - dt.timedelta(days=d.weekday())
    if name == "month":
        return d.replace(day=1)
    if name == "year":
        return d.replace(month=1, day=1)
    return d


def _parents(d: dt.date) -> list[tuple[str, dt.date]]:
    return [(n, _start(n, d)) for n in ("week", "month", "year")]


def make_plan(src: SiteData, tgt: SiteData, on_conflict: str = "abort") -> Plan:
    """on_conflict: 'abort' (report), 'keep-target' (drop S's archive-only day)."""
    plan = Plan()
    plan.move_raw_days = sorted(d for d, n in src.raw_visits.items() if n > 0)
    raw_moved = set(plan.move_raw_days)

    archive_moved_days: set[dt.date] = set()
    for p in sorted(src.archives):
        if p.period != DAY:
            continue
        d = p.date1
        if d in raw_moved:
            continue  # rebuilt from raw logs
        if src.archives[p] == 0:
            continue  # nothing to carry over
        if tgt.has_data(d):
            if on_conflict == "keep-target":
                plan.keep_target.append(p)
                continue
            plan.conflicts.append(Conflict(
                p, f"S has only an archive ({src.archives[p]} visits) and T has "
                   f"{tgt.day_visits(d)} visits on this day"))
            continue
        plan.move_archives.append(p)
        archive_moved_days.add(d)

    changed_days = raw_moved | archive_moved_days
    parents_to_invalidate: set[Period] = set()
    for p in sorted(src.archives):
        if p.period in (DAY, RANGE):
            continue
        days = list(p.days())
        src_raw_inside = any(d in raw_moved for d in days)
        tgt_inside = any(tgt.has_data(d) for d in days)
        if not src_raw_inside and not tgt_inside:
            if src.archives[p] > 0 or any(d in archive_moved_days for d in days):
                plan.move_archives.append(p)
            continue
        # Rebuilt from days: S's days must account for all of S's visits.
        day_sum = sum(src.day_visits(d) for d in days
                      if d in raw_moved or d in archive_moved_days)
        if src.archives[p] > day_sum:
            missing = src.archives[p] - day_sum
            if on_conflict == "keep-target":
                plan.keep_target.append(p)
                plan.notes.append(f"{p}: {missing} visits of S exist only in the "
                                  f"{PERIOD_NAMES[p.period]} archive and are dropped")
            else:
                plan.conflicts.append(Conflict(
                    p, f"S's {PERIOD_NAMES[p.period]} archive has {src.archives[p]} visits "
                       f"but its days only {day_sum}; T has data in this period, so "
                       f"Matomo would rebuild it from days and lose {missing} visits"))
        parents_to_invalidate.add(p)

    moved = set(plan.move_archives)
    inv: set[tuple[str, dt.date]] = set()
    for d in raw_moved:
        inv.add(("day", d))  # Matomo cascades up to week, month, year
    for d in archive_moved_days:
        for name, pd in _parents(d):
            inv.add((name, pd))
    for p in parents_to_invalidate:
        inv.add((PERIOD_NAMES[p.period], _start(PERIOD_NAMES[p.period], p.date1)))
    # Do not invalidate a parent whose archive is being moved as a whole.
    plan.invalidate = sorted(
        (name, d) for name, d in inv
        if not any(m.period != DAY and PERIOD_NAMES[m.period] == name
                   and m.date1 <= d <= m.date2 for m in moved)
    )
    plan.move_archives.sort()
    if changed_days:
        plan.notes.append(f"{len(changed_days)} days change for T "
                          f"({min(changed_days)}..{max(changed_days)})")
    return plan
