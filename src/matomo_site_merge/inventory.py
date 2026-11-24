"""Read what a site has from the Matomo database."""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from .mapping import Dimension, Goal
from .planner import DAY, Period, SiteData

VALID_DONE = (1, 3)  # ArchiveWriter::DONE_OK, DONE_OK_TEMPORARY
DONE_NAMES = ("done", "done.VisitsSummary")


@dataclass
class Site:
    idsite: int
    name: str
    timezone: str
    ts_created: dt.datetime
    currency: str
    ecommerce: int
    main_url: str


class Db:
    def __init__(self, conn, prefix: str):
        self.conn = conn
        self.prefix = prefix

    def t(self, name: str) -> str:
        return f"`{self.prefix}{name}`"

    def q(self, sql: str, args=()) -> list[dict]:
        with self.conn.cursor() as c:
            c.execute(sql, args)
            return list(c.fetchall())

    def x(self, sql: str, args=()) -> int:
        with self.conn.cursor() as c:
            return c.execute(sql, args)

    def tables(self, like: str) -> list[str]:
        rows = self.q("SHOW TABLES LIKE %s", (self.prefix + like,))
        return sorted(list(r.values())[0][len(self.prefix):] for r in rows)

    def columns(self, table: str) -> list[str]:
        return [r["Field"] for r in self.q(f"SHOW COLUMNS FROM {self.t(table)}")]


def tzinfo(name: str) -> dt.tzinfo:
    """Matomo stores either an IANA name or a manual offset such as UTC+5.5."""
    if name in ("UTC", "GMT"):
        return dt.timezone.utc
    m = re.fullmatch(r"UTC([+-])(\d+(?:\.\d+)?)", name)
    if m:
        hours = float(m.group(2)) * (1 if m.group(1) == "+" else -1)
        return dt.timezone(dt.timedelta(hours=hours))
    return ZoneInfo(name)


def load_site(db: Db, idsite: int) -> Site:
    rows = db.q(f"SELECT idsite, name, timezone, ts_created, currency, ecommerce, main_url "
                f"FROM {db.t('site')} WHERE idsite=%s", (idsite,))
    if not rows:
        raise SystemExit(f"site {idsite} does not exist")
    return Site(**rows[0])


def raw_visits_per_day(db: Db, site: Site) -> dict[dt.date, int]:
    """Visits per local day, as Matomo assigns them (by visit_last_action_time)."""
    tz = tzinfo(site.timezone)
    rows = db.q(
        f"SELECT FLOOR(UNIX_TIMESTAMP(visit_last_action_time)/900) AS q, COUNT(*) AS n "
        f"FROM {db.t('log_visit')} WHERE idsite=%s GROUP BY q", (site.idsite,))
    out: dict[dt.date, int] = {}
    for r in rows:
        # UNIX_TIMESTAMP interprets the DATETIME in the session time zone; the
        # connection sets time_zone='+00:00', matching how Matomo stores UTC.
        local = dt.datetime.fromtimestamp(int(r["q"]) * 900, dt.timezone.utc).astimezone(tz)
        out[local.date()] = out.get(local.date(), 0) + int(r["n"])
    return out


def archive_tables(db: Db) -> list[str]:
    return db.tables("archive_numeric_%")


def blob_table_for(numeric: str) -> str:
    return numeric.replace("archive_numeric_", "archive_blob_")


def archive_table_for(d: dt.date) -> str:
    return f"archive_numeric_{d.year:04d}_{d.month:02d}"


def archived_periods(db: Db, idsite: int) -> dict[Period, int]:
    """nb_visits of the newest valid all-visits archive per period (no segment)."""
    best: dict[Period, tuple[dt.datetime, int]] = {}
    for t in archive_tables(db):
        rows = db.q(
            f"SELECT d.period, d.date1, d.date2, d.ts_archived, v.value AS visits "
            f"FROM {db.t(t)} d LEFT JOIN {db.t(t)} v "
            f"  ON v.idarchive = d.idarchive AND v.name = 'nb_visits' "
            f"WHERE d.idsite=%s AND d.name IN %s AND d.value IN %s",
            (idsite, DONE_NAMES, VALID_DONE))
        for r in rows:
            p = Period(int(r["period"]), r["date1"], r["date2"])
            visits = int(r["visits"] or 0)
            prev = best.get(p)
            if prev is None or r["ts_archived"] > prev[0]:
                best[p] = (r["ts_archived"], visits)
    return {p: v for p, (_, v) in best.items()}


def site_data(db: Db, site: Site) -> SiteData:
    return SiteData(raw_visits=raw_visits_per_day(db, site),
                    archives=archived_periods(db, site.idsite))


def goals(db: Db, idsite: int) -> list[Goal]:
    rows = db.q(f"SELECT idgoal, name, match_attribute, pattern FROM {db.t('goal')} "
                f"WHERE idsite=%s AND deleted=0", (idsite,))
    return [Goal(int(r["idgoal"]), r["name"], r["match_attribute"] or "", r["pattern"] or "")
            for r in rows]


def dimensions(db: Db, idsite: int) -> list[Dimension]:
    if not db.tables("custom_dimensions"):
        return []
    rows = db.q(f"SELECT idcustomdimension, name, scope, `index` FROM "
                f"{db.t('custom_dimensions')} WHERE idsite=%s", (idsite,))
    return [Dimension(int(r["idcustomdimension"]), r["name"], r["scope"], int(r["index"]))
            for r in rows]


def dimension_slots(db: Db) -> dict[str, list[int]]:
    """Column indexes custom_dimension_<n> that exist, per scope."""
    out = {}
    for scope, table in (("visit", "log_visit"), ("action", "log_link_visit_action")):
        out[scope] = sorted(int(c.rsplit("_", 1)[1]) for c in db.columns(table)
                            if re.fullmatch(r"custom_dimension_\d+", c))
    return out


def has_archives_with_goals(db: Db, idsite: int, periods: list[Period]) -> set[int]:
    """Goal IDs that appear in numeric archive names of the given S periods."""
    found: set[int] = set()
    by_table: dict[str, list[Period]] = {}
    for p in periods:
        by_table.setdefault(archive_table_for(p.date1), []).append(p)
    existing = set(archive_tables(db))
    for t, ps in by_table.items():
        if t not in existing:
            continue
        for p in ps:
            rows = db.q(
                f"SELECT DISTINCT name FROM {db.t(t)} WHERE idsite=%s AND period=%s "
                f"AND date1=%s AND date2=%s AND name LIKE 'Goal\\_%%'",
                (idsite, p.period, p.date1, p.date2))
            for r in rows:
                m = re.match(r"Goal_(\d+)_", r["name"])
                if m:
                    found.add(int(m.group(1)))
    return found


def has_archives_with_dims(db: Db, idsite: int, periods: list[Period]) -> set[int]:
    found: set[int] = set()
    existing = set(archive_tables(db))
    for p in periods:
        t = blob_table_for(archive_table_for(p.date1))
        if archive_table_for(p.date1) not in existing:
            continue
        rows = db.q(
            f"SELECT DISTINCT name FROM {db.t(t)} WHERE idsite=%s AND period=%s "
            f"AND date1=%s AND date2=%s AND name LIKE 'CustomDimensions\\_Dimension%%'",
            (idsite, p.period, p.date1, p.date2))
        for r in rows:
            m = re.match(r"CustomDimensions_Dimension(\d+)", r["name"])
            if m:
                found.add(int(m.group(1)))
    return found


__all__ = ["Db", "Site", "load_site", "site_data", "goals", "dimensions", "dimension_slots",
           "archive_tables", "blob_table_for", "archive_table_for", "DAY"]
