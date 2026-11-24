"""Apply and undo a merge. Every change is recorded in a JSON journal first."""
from __future__ import annotations

import datetime as dt
import json
import shlex
import subprocess
from pathlib import Path

from .inventory import Db, Site, archive_table_for, archive_tables, blob_table_for
from .mapping import Dimension, Goal, column_moves
from .planner import PERIOD_NAMES, RANGE, Period, Plan

BATCH = 5000
RAW_TABLES = ("log_visit", "log_link_visit_action", "log_conversion", "log_conversion_item")


class Journal:
    def __init__(self, path: Path, data: dict):
        self.path = path
        self.data = data
        self.save()

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, default=str, indent=1))
        tmp.replace(self.path)

    def step(self, name: str, **info) -> None:
        self.data["steps"].append({"step": name, **info})
        self.save()

    @classmethod
    def load(cls, path: Path) -> "Journal":
        j = cls.__new__(cls)
        j.path = path
        j.data = json.loads(path.read_text())
        return j


def _copy_row(db: Db, table: str, where: str, args: tuple, changes: dict) -> None:
    rows = db.q(f"SELECT * FROM {db.t(table)} WHERE {where}", args)
    if len(rows) != 1:
        raise RuntimeError(f"expected one row in {table} for {args}, got {len(rows)}")
    row = {**rows[0], **changes}
    cols = ", ".join(f"`{c}`" for c in row)
    marks = ", ".join(["%s"] * len(row))
    db.x(f"INSERT INTO {db.t(table)} ({cols}) VALUES ({marks})", tuple(row.values()))


def remap_goals(db: Db, idsite: int, mapping: dict[int, int], visits: list[int] | None = None) -> int:
    """Renumber idgoal of conversions; two passes so swaps cannot collide on the key."""
    moves = {s: t for s, t in mapping.items() if s != t}
    if not moves:
        return 0
    scope = "idsite=%s"
    args: tuple = (idsite,)
    if visits is not None:
        scope += " AND idvisit IN %s"
        args += (tuple(visits),)
    n = 0
    off = 1_000_000
    for s in moves:
        n += db.x(f"UPDATE {db.t('log_conversion')} SET idgoal=%s WHERE {scope} AND idgoal=%s",
                  (s + off,) + args + (s,))
    for s, t in moves.items():
        db.x(f"UPDATE {db.t('log_conversion')} SET idgoal=%s WHERE {scope} AND idgoal=%s",
             (t,) + args + (s + off,))
    return n


def move_dim_columns(db: Db, idsite: int, moves: dict[str, dict[int, int]],
                     visits: list[int] | None = None) -> None:
    """Copy custom_dimension_<s> to custom_dimension_<t> for the site's rows.

    Values are read from a temporary copy, so chains and swaps of slots work
    (MySQL evaluates single-table UPDATE assignments left to right)."""
    targets = {"visit": ("log_visit", "log_conversion"), "action": ("log_link_visit_action",)}
    for scope, m in moves.items():
        m = {s: t for s, t in m.items() if s != t}
        if not m:
            continue
        for table in targets[scope]:
            key = "idlink_va" if table == "log_link_visit_action" else (
                "idvisit" if table == "log_visit" else None)
            cols = sorted(set(m) | set(m.values()))
            src_cols = ", ".join(f"custom_dimension_{c}" for c in cols)
            where, args = "idsite=%s", (idsite,)
            if visits is not None:
                where += " AND idvisit IN %s"
                args += (tuple(visits),)
            if key is None:  # log_conversion: (idvisit, idgoal, buster)
                keycols = ("idvisit", "idgoal", "buster")
            else:
                keycols = (key,)
            kc = ", ".join(keycols)
            db.x("DROP TEMPORARY TABLE IF EXISTS msm_dims")
            db.x(f"CREATE TEMPORARY TABLE msm_dims AS SELECT {kc}, {src_cols} "
                 f"FROM {db.t(table)} WHERE {where}", args)
            sets = [f"v.custom_dimension_{t} = o.custom_dimension_{s}" for s, t in m.items()]
            cleared = set(m) - set(m.values())
            sets += [f"v.custom_dimension_{c} = NULL" for c in sorted(cleared)]
            on = " AND ".join(f"v.{k} = o.{k}" for k in keycols)
            db.x(f"UPDATE {db.t(table)} v JOIN msm_dims o ON {on} SET {', '.join(sets)}")
            db.x("DROP TEMPORARY TABLE msm_dims")


def _archive_ids(db: Db, idsite: int, p: Period) -> tuple[str, list[int]]:
    t = archive_table_for(p.date1)
    if t not in set(archive_tables(db)):
        return t, []
    ids = set()
    for table in (t, blob_table_for(t)):
        for r in db.q(f"SELECT DISTINCT idarchive FROM {db.t(table)} WHERE idsite=%s AND "
                      f"period=%s AND date1=%s AND date2=%s",
                      (idsite, p.period, p.date1, p.date2)):
            ids.add(int(r["idarchive"]))
    return t, sorted(ids)


def _set_archive_site(db: Db, table: str, ids: list[int], idsite: int) -> None:
    if ids:
        for t in (table, blob_table_for(table)):
            db.x(f"UPDATE {db.t(t)} SET idsite=%s WHERE idarchive IN %s", (idsite, tuple(ids)))


def _delete_archives(db: Db, table: str, ids: list[int]) -> None:
    if ids:
        for t in (table, blob_table_for(table)):
            db.x(f"DELETE FROM {db.t(t)} WHERE idarchive IN %s", (tuple(ids),))


def console_commands(console: str | None, idsite: int, invalidate: list[tuple[str, dt.date]]
                     ) -> list[list[str]]:
    base = shlex.split(console) if console else ["./console"]
    by_period: dict[str, list[str]] = {}
    for name, d in invalidate:
        by_period.setdefault(name, []).append(d.isoformat())
    cmds = []
    for name in ("day", "week", "month", "year"):
        dates = by_period.get(name)
        if not dates:
            continue
        for i in range(0, len(dates), 200):
            cmd = base + ["core:invalidate-report-data", f"--sites={idsite}", f"--periods={name}"]
            if name != "day":
                # Parents of archive-only days are older than the log purge
                # threshold by definition; their days are not invalidated.
                cmd.append("--ignore-log-deletion-limit")
            cmd += [f"--dates={d}" for d in dates[i:i + 200]]
            cmds.append(cmd)
    cmds.append(base + ["cache:clear"])
    return cmds


def run_console(cmds: list[list[str]], log) -> None:
    for cmd in cmds:
        log("$ " + " ".join(shlex.quote(c) for c in cmd[:6]) + (" ..." if len(cmd) > 6 else ""))
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        if r.returncode != 0:
            raise RuntimeError(f"console command failed ({r.returncode}): {r.stdout}{r.stderr}")


def apply(db: Db, src: Site, tgt: Site, plan: Plan, goal_map: dict[int, int],
          create_goals: list[Goal], dim_map: dict[int, int], create_dims: list[Dimension],
          src_dims: list[Dimension], tgt_dims: list[Dimension], journal: Journal,
          move_segments: bool, log) -> None:
    S, T = src.idsite, tgt.idsite
    conn = db.conn

    # 1. Goals and dimensions T needs.
    for g in create_goals:
        _copy_row(db, "goal", "idsite=%s AND idgoal=%s",
                  (S, next(s for s, t in goal_map.items() if t == g.idgoal)),
                  {"idsite": T, "idgoal": g.idgoal})
    for d in create_dims:
        s_id = next(s for s, t in dim_map.items() if t == d.iddim)
        _copy_row(db, "custom_dimensions", "idsite=%s AND idcustomdimension=%s", (S, s_id),
                  {"idsite": T, "idcustomdimension": d.iddim, "index": d.index})
    conn.commit()
    journal.step("created", goals=[g.idgoal for g in create_goals],
                 dims=[d.iddim for d in create_dims])
    log(f"created in T: {len(create_goals)} goals, {len(create_dims)} custom dimensions")

    # 2. Raw logs, batch by batch: renumber goals and dimension columns, then move.
    moves = column_moves(src_dims, tgt_dims + create_dims, dim_map)
    total = 0
    while True:
        ids = [int(r["idvisit"]) for r in db.q(
            f"SELECT idvisit FROM {db.t('log_visit')} WHERE idsite=%s ORDER BY idvisit LIMIT %s",
            (S, BATCH))]
        if not ids:
            break
        journal.step("raw_batch_start", visits=ids)
        remap_goals(db, S, goal_map, ids)
        move_dim_columns(db, S, moves, ids)
        for table in RAW_TABLES:
            db.x(f"UPDATE {db.t(table)} SET idsite=%s WHERE idsite=%s AND idvisit IN %s",
                 (T, S, tuple(ids)))
        conn.commit()
        journal.step("raw_batch_done", count=len(ids))
        total += len(ids)
        log(f"moved {total} visits")
    # Rows whose visit was purged earlier (orphans) follow their site.
    orphans = {}
    for table in RAW_TABLES[1:]:
        n = db.x(f"UPDATE {db.t(table)} SET idsite=%s WHERE idsite=%s", (T, S))
        if n:
            orphans[table] = n
    conn.commit()
    if orphans:
        journal.step("orphans", rows=orphans)

    # 3. Archives that move as they are.
    moved = []
    for p in plan.move_archives:
        table, s_ids = _archive_ids(db, S, p)
        _, t_ids = _archive_ids(db, T, p)
        journal.step("archive_move", table=table, period=[p.period, p.date1, p.date2],
                     source_ids=s_ids, deleted_target_ids=t_ids)
        _delete_archives(db, table, t_ids)
        _set_archive_site(db, table, s_ids, T)
        conn.commit()
        moved.append(p)
    log(f"moved {len(moved)} archived periods")

    # 4. T's range archives that overlap a changed day are stale.
    changed = set(plan.move_raw_days) | {p.date1 for p in plan.move_archives if p.period == 1}
    if changed:
        lo, hi = min(changed), max(changed)
        n = 0
        for t in archive_tables(db):
            ids = [int(r["idarchive"]) for r in db.q(
                f"SELECT DISTINCT idarchive FROM {db.t(t)} WHERE idsite=%s AND period=%s "
                f"AND date2 >= %s AND date1 <= %s", (T, RANGE, lo, hi))]
            _delete_archives(db, t, ids)
            n += len(ids)
        conn.commit()
        journal.step("ranges_deleted", count=n)

    # 5. Site settings.
    first = min(src.ts_created, tgt.ts_created)
    db.x(f"UPDATE {db.t('site')} SET ts_created=%s WHERE idsite=%s", (first, T))
    seg = 0
    seg_ids: list[int] = []
    if move_segments:
        seg_ids = [int(r["idsegment"]) for r in db.q(
            f"SELECT idsegment FROM {db.t('segment')} WHERE enable_only_idsite=%s AND deleted=0",
            (S,))]
        seg = db.x(f"UPDATE {db.t('segment')} SET enable_only_idsite=%s "
                   f"WHERE enable_only_idsite=%s AND deleted=0", (T, S))
    conn.commit()
    journal.step("site", old_ts_created=tgt.ts_created, segments_moved=seg, segment_ids=seg_ids)


def undo(db: Db, journal: Journal, log) -> None:
    d = journal.data
    S, T = d["source"], d["target"]
    goal_map = {int(k): v for k, v in d["goal_map"].items()}
    inverse_goals = {t: s for s, t in goal_map.items()}
    dim_moves = {scope: {int(k): v for k, v in m.items()} for scope, m in d["dim_moves"].items()}
    inverse_dims = {scope: {t: s for s, t in m.items()} for scope, m in dim_moves.items()}
    conn = db.conn
    steps = d["steps"]
    for i in range(len(steps) - 1, -1, -1):
        st = steps[i]
        kind = st["step"]
        if kind == "raw_batch_start":
            # A batch is one transaction; without its "done" entry it did not commit.
            if i + 1 >= len(steps) or steps[i + 1]["step"] != "raw_batch_done":
                continue
            ids = st["visits"]
            for table in RAW_TABLES:
                db.x(f"UPDATE {db.t(table)} SET idsite=%s WHERE idsite=%s AND idvisit IN %s",
                     (S, T, tuple(ids)))
            move_dim_columns(db, S, inverse_dims, ids)
            remap_goals(db, S, inverse_goals, ids)
            conn.commit()
        elif kind == "archive_move":
            if st["source_ids"]:
                _set_archive_site(db, st["table"], st["source_ids"], S)
            conn.commit()
        elif kind == "site":
            db.x(f"UPDATE {db.t('site')} SET ts_created=%s WHERE idsite=%s",
                 (st["old_ts_created"], T))
            if st["segments_moved"]:
                for sid in st["segment_ids"]:
                    db.x(f"UPDATE {db.t('segment')} SET enable_only_idsite=%s WHERE idsegment=%s",
                         (S, sid))
            conn.commit()
        elif kind == "created":
            for g in st["goals"]:
                db.x(f"DELETE FROM {db.t('goal')} WHERE idsite=%s AND idgoal=%s", (T, g))
            for dim in st["dims"]:
                db.x(f"DELETE FROM {db.t('custom_dimensions')} WHERE idsite=%s AND "
                     f"idcustomdimension=%s", (T, dim))
            conn.commit()
        elif kind == "orphans":
            log("note: orphan log rows without a visit stay in T (they were not listed)")
    log("undo done; invalidate and re-archive both sites for the affected days")


__all__ = ["Journal", "apply", "undo", "console_commands", "run_console", "PERIOD_NAMES"]
