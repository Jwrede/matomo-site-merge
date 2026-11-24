"""matomo-site-merge: merge the data of one Matomo site into another."""
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sys
from pathlib import Path

from . import inventory as inv
from .executor import Journal, apply, console_commands, run_console, undo
from .mapping import column_moves, map_dims, map_goals
from .planner import DAY, MONTH, PERIOD_NAMES, make_plan


def log(msg: str) -> None:
    print(msg, flush=True)


def read_matomo_config(path: str) -> dict:
    """The [database] section of config/config.ini.php."""
    out, section = {}, None
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        m = re.fullmatch(r"\[(\w+)\]", line)
        if m:
            section = m.group(1)
            continue
        if section == "database" and "=" in line and not line.startswith(";"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"')
    return out


def connect(a) -> inv.Db:
    import pymysql
    import pymysql.cursors
    cfg = read_matomo_config(a.matomo_config) if a.matomo_config else {}
    conn = pymysql.connect(
        host=a.host or cfg.get("host", "127.0.0.1"),
        port=int(a.port or cfg.get("port", 3306)),
        user=a.user or cfg.get("username", "matomo"),
        password=os.environ.get("MATOMO_DB_PASSWORD", cfg.get("password", "")),
        database=a.database or cfg.get("dbname", "matomo"),
        charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor, autocommit=False,
        init_command="SET time_zone='+00:00'")
    prefix = a.prefix if a.prefix is not None else cfg.get("tables_prefix", "matomo_")
    return inv.Db(conn, prefix)


def parse_pairs(values: list[str], flag: str) -> dict[int, int]:
    out = {}
    for v in values or []:
        m = re.fullmatch(r"(\d+)=(\d+)", v)
        if not m:
            raise SystemExit(f"{flag} expects SRC=TGT, got {v!r}")
        out[int(m.group(1))] = int(m.group(2))
    return out


def build(db: inv.Db, a):
    if a.source == a.target:
        raise SystemExit("source and target are the same site")
    src, tgt = inv.load_site(db, a.source), inv.load_site(db, a.target)
    problems = []
    if src.timezone != tgt.timezone:
        problems.append(f"time zones differ (S {src.timezone}, T {tgt.timezone}): archived days "
                        f"of S would land on T's calendar shifted; set the same time zone first")
    if src.currency != tgt.currency:
        problems.append(f"currencies differ (S {src.currency}, T {tgt.currency}): revenue "
                        f"would be added up without conversion")
    sd, td = inv.site_data(db, src), inv.site_data(db, tgt)
    plan = make_plan(sd, td, a.on_conflict)
    moved_periods = plan.move_archives
    goal_ids = inv.has_archives_with_goals(db, src.idsite, moved_periods)
    dim_ids = inv.has_archives_with_dims(db, src.idsite, moved_periods)
    sg, tg = inv.goals(db, src.idsite), inv.goals(db, tgt.idsite)
    goal_map, create_goals, gerr = map_goals(sg, tg, parse_pairs(a.goal, "--goal"), False)
    for g in sg:  # identity only matters for goals the moved archives mention
        if g.idgoal in goal_ids and goal_map.get(g.idgoal) != g.idgoal:
            gerr.append(f"goal {g.idgoal} '{g.name}' maps to T goal {goal_map.get(g.idgoal)}, but "
                        f"{len(moved_periods)} archived periods that move store it as goal "
                        f"{g.idgoal}; archives cannot be renumbered")
    sdims, tdims = inv.dimensions(db, src.idsite), inv.dimensions(db, tgt.idsite)
    slots = inv.dimension_slots(db)
    used = {s: {d.index for d in tdims if d.scope == s} for s in ("visit", "action")}
    free = {s: [i for i in slots[s] if i not in used[s]] for s in slots}
    dim_map, create_dims, derr = map_dims(sdims, tdims, parse_pairs(a.dimension, "--dimension"),
                                          False, free)
    for d in sdims:
        if d.iddim in dim_ids and dim_map.get(d.iddim) != d.iddim:
            derr.append(f"custom dimension {d.iddim} '{d.name}' maps to T dimension "
                        f"{dim_map.get(d.iddim)}, but archives that move store it as {d.iddim}")
    return src, tgt, sd, td, plan, goal_map, create_goals, dim_map, create_dims, sdims, tdims, \
        problems + gerr + derr


def print_plan(src, tgt, sd, td, plan, goal_map, create_goals, dim_map, create_dims, errors):
    log(f"merge site {src.idsite} '{src.name}' into site {tgt.idsite} '{tgt.name}'")
    raw = sum(sd.raw_visits.values())
    log(f"  raw logs:  {raw} visits of S on {len(plan.move_raw_days)} days move to T "
        f"and are re-archived")
    days = [p for p in plan.move_archives if p.period == DAY]
    log(f"  archives:  {len(plan.move_archives)} archived periods of S move as they are "
        f"({len(days)} days with {sum(sd.archives[p] for p in days)} visits whose raw logs are gone)")
    counts = {}
    for name, _ in plan.invalidate:
        counts[name] = counts.get(name, 0) + 1
    log("  invalidate for T: " + (", ".join(f"{k} x{n}" for k, n in counts.items()) or "nothing"))
    for s, t in sorted(goal_map.items()):
        new = " (created in T)" if any(g.idgoal == t for g in create_goals) else ""
        log(f"  goal {s} -> {t}{new}")
    for s, t in sorted(dim_map.items()):
        new = " (created in T)" if any(d.iddim == t for d in create_dims) else ""
        log(f"  custom dimension {s} -> {t}{new}")
    for p in plan.keep_target:
        log(f"  keep T, drop S: {p}")
    for n in plan.notes:
        log(f"  note: {n}")
    for c in plan.conflicts[:50]:
        log(f"  CONFLICT {c.period}: {c.reason}")
    if len(plan.conflicts) > 50:
        log(f"  ... {len(plan.conflicts) - 50} more conflicts")
    for e in errors:
        log(f"  ERROR {e}")
    if plan.conflicts:
        log("  conflicts can be resolved with --on-conflict keep-target (S's data for those "
            "periods is dropped)")


def expected_after(sd, td) -> dict[str, int]:
    """Visits T should report per day and month once Matomo has re-archived."""
    days = set(sd.raw_visits) | set(td.raw_visits) | \
        {p.date1 for p in sd.archives if p.period == DAY} | \
        {p.date1 for p in td.archives if p.period == DAY}
    out: dict[str, int] = {}
    months: dict[str, int] = {}
    for d in sorted(days):
        n = sd.day_visits(d) + td.day_visits(d)
        if n:
            out[f"day {d}"] = n
            months[f"month {d:%Y-%m}"] = months.get(f"month {d:%Y-%m}", 0) + n
    out.update(months)
    return out


def cmd_plan(a) -> int:
    db = connect(a)
    r = build(db, a)
    print_plan(*r[:9], r[11])
    return 1 if (r[4].conflicts or r[11]) else 0


def cmd_apply(a) -> int:
    db = connect(a)
    (src, tgt, sd, td, plan, goal_map, create_goals, dim_map, create_dims, sdims, tdims,
     errors) = build(db, a)
    print_plan(src, tgt, sd, td, plan, goal_map, create_goals, dim_map, create_dims, errors)
    if plan.conflicts or errors:
        log("not applied: resolve the conflicts and errors above first")
        return 1
    if not a.yes:
        log("dry run; add --yes to apply")
        return 0
    jpath = Path(a.journal or f"matomo-merge-{src.idsite}-into-{tgt.idsite}-"
                 f"{dt.datetime.now():%Y%m%d-%H%M%S}.json")
    moves = column_moves(sdims, tdims + create_dims, dim_map)
    journal = Journal(jpath, {
        "source": src.idsite, "target": tgt.idsite, "prefix": db.prefix,
        "goal_map": goal_map, "dim_moves": moves,
        "invalidate": [[n, d.isoformat()] for n, d in plan.invalidate],
        "expected": expected_after(sd, td), "steps": []})
    log(f"journal: {jpath}")
    apply(db, src, tgt, plan, goal_map, create_goals, dim_map, create_dims, sdims, tdims,
          journal, a.move_segments, log)
    cmds = console_commands(a.console, tgt.idsite, plan.invalidate)
    if a.console:
        run_console(cmds, log)
        journal.step("invalidated")
        log("invalidated; run core:archive (or let the cron do it), then `verify`")
    else:
        log("now run in the Matomo directory:")
        for c in cmds:
            log("  " + " ".join(c))
        log("then core:archive, then `matomo-site-merge verify --journal ...`")
    log(f"site {src.idsite} now has no raw data; delete it in Matomo once verify is clean")
    return 0


def cmd_undo(a) -> int:
    db = connect(a)
    j = Journal.load(Path(a.journal))
    undo(db, j, log)
    inv_days = [(n, dt.date.fromisoformat(d)) for n, d in j.data["invalidate"]]
    for site in (j.data["source"], j.data["target"]):
        cmds = console_commands(a.console, site, inv_days)
        if a.console:
            run_console(cmds, log)
        else:
            for c in cmds:
                log("  " + " ".join(c))
    j.step("undone")
    return 0


def cmd_verify(a) -> int:
    db = connect(a)
    j = Journal.load(Path(a.journal))
    archived = inv.archived_periods(db, j.data["target"])
    by_key = {}
    for p, n in archived.items():
        if p.period == DAY:
            by_key[f"day {p.date1}"] = n
        elif p.period == MONTH:
            by_key[f"month {p.date1:%Y-%m}"] = n
    bad = 0
    for k, want in j.data["expected"].items():
        got = by_key.get(k)
        if got != want:
            bad += 1
            if bad <= 50:
                log(f"MISMATCH {k}: expected {want} visits, T reports {got}")
    checked = len(j.data["expected"])
    log(f"verify: {checked - bad} of {checked} days and months match")
    return 1 if bad else 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="matomo-site-merge", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def db_args(sp):
        sp.add_argument("--matomo-config", help="path to config/config.ini.php (reads [database])")
        sp.add_argument("--host")
        sp.add_argument("--port")
        sp.add_argument("--user")
        sp.add_argument("--database")
        sp.add_argument("--prefix", help="table prefix (default matomo_)")
        sp.add_argument("--console", help="command that runs Matomo's console, e.g. "
                        "'php /var/www/matomo/console'; without it the commands are printed")

    for name in ("plan", "apply"):
        sp = sub.add_parser(name)
        db_args(sp)
        sp.add_argument("--source", type=int, required=True)
        sp.add_argument("--target", type=int, required=True)
        sp.add_argument("--goal", action="append", metavar="SRC=TGT")
        sp.add_argument("--dimension", action="append", metavar="SRC=TGT")
        sp.add_argument("--on-conflict", choices=("abort", "keep-target"), default="abort")
        sp.add_argument("--no-move-segments", dest="move_segments", action="store_false")
        if name == "apply":
            sp.add_argument("--yes", action="store_true")
            sp.add_argument("--journal")
    for name in ("undo", "verify"):
        sp = sub.add_parser(name)
        db_args(sp)
        sp.add_argument("--journal", required=True)
    a = p.parse_args(argv)
    return {"plan": cmd_plan, "apply": cmd_apply, "undo": cmd_undo, "verify": cmd_verify}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
