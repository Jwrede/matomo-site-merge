# matomo-site-merge

Merge the data of one Matomo site (idsite) into another on the same Matomo
installation: raw visit logs, archived reports whose raw logs are gone, goals
and custom dimensions. Every change is written to a journal first, so a merge
can be checked with `verify` and reverted with `undo`.

Typical cases:

* A relaunch was tracked into a new site, and the history of the old site
  should continue in the new one.
* History was imported from Google Analytics into a separate site (the
  importer creates archives only, no raw logs).
* The same website was tracked twice, under two site IDs, for some time.

## Why

Matomo has no way to do this. Its FAQ says: "it is not possible to move data
between multiple idsite within Matomo or merge the data"
([FAQ](https://matomo.org/faq/on-premise/can-you-merge-data-from-two-idsite-into-one/)).
The only supported route is the paid Roll-Up Reporting plugin, which adds a
third, aggregated site and leaves both sites as they are.

* [matomo-org/matomo#7650](https://github.com/matomo-org/matomo/issues/7650)
  "Merge data from two sites into one" is open since April 2015, with 46
  reactions and 38 comments (November 2026). The comments describe hand-written
  SQL (update `idsite` in the log tables, then in `archive_numeric_*` and
  `archive_blob_*`, then fix `ts_created`), lists of tables to check, and
  warnings that it breaks when goal IDs differ or when only archives exist.
  The Google Analytics importer made the problem more common, because it
  always imports into a new site.
* The Matomo forum has the same question in many threads, for example
  [Merging two sets of website data](https://forum.matomo.org/t/merging-two-sets-of-website-data/14084),
  [Merging Matomo Analytics from one site into another](https://forum.matomo.org/t/merging-matomo-analytics-from-one-site-into-another/42431),
  [Best practice: preserve analytics history across relaunch](https://forum.matomo.org/t/best-practice-preserve-analytics-history-across-relaunch-when-a-new-matomo-site-is-created/64817)
  and a [tutorial with the SQL steps](https://forum.matomo.org/t/tutorial-for-merge-2-site-ids-data-working-method/50027).
* The Migration plugin copies a site to another Matomo installation; it does
  not merge into an existing site.

I did not find a maintained tool for it (GitHub search for "matomo merge site"
and variants returns nothing).

## What it does

`matomo-site-merge plan` reads both sites from the database and decides, per
archived period, what has to happen. `apply` does it.

| Source (S) has | Target (T) has | Result |
|---|---|---|
| raw logs for a day | anything | raw logs move to T; T's day is invalidated and Matomo re-archives it from the combined logs |
| only an archive for a day (raw logs purged, or imported from GA) | no data that day | the archive moves to T unchanged |
| only an archive for a day | data that day | **conflict**: two archives cannot be added up without their raw data. `--on-conflict keep-target` keeps T's day and drops S's |
| a week, month or year archive, no data of T or raw logs of S inside | | the archive moves to T unchanged |
| a week, month or year archive, T has data inside | | T's period is invalidated and rebuilt from the days. If S's period archive counts more visits than its day archives (old day reports deleted), that is a conflict |

Further steps:

* **Goals.** Goals are matched by name. Raw conversions of S are renumbered to
  T's goal IDs. A goal T does not have is created in T, with S's ID if that is
  free. Archives cannot be renumbered (their blobs are compressed, serialized
  PHP), so for archives that move, a goal they mention must keep its ID; if it
  would not, `plan` reports an error. `--goal SRC=TGT` overrides the match.
* **Custom dimensions.** Matched by name and scope. Values in the raw log
  columns (`custom_dimension_<n>`) are moved to T's slot of the dimension,
  through a temporary copy so swapped slots work. Dimensions T lacks are
  created in a free slot. Same ID rule as goals for moved archives.
  `--dimension SRC=TGT` overrides.
* Visit-level rows in `log_visit`, `log_link_visit_action`, `log_conversion`
  and `log_conversion_item` move in batches of 5000 visits, one transaction
  per batch.
* T's range archives that overlap a changed day are deleted (Matomo recomputes
  ranges on request).
* T's creation date (`ts_created`) is set to the earlier of the two, otherwise
  Matomo hides the older reports.
* Segments restricted to S are restricted to T (`--no-move-segments` to keep).
* Invalidation runs through Matomo's own `core:invalidate-report-data`
  (`--console`), or the commands are printed. Week, month and year
  invalidations use `--ignore-log-deletion-limit`, because their days are
  older than the log purge threshold by definition; days without raw logs are
  never invalidated.
* `verify` compares T's archived visits per day and month after re-archiving
  with the sum of both sites before the merge, as stored in the journal.
* `undo` moves the journaled visits and archives back, reverses the goal and
  dimension renumbering, removes created goals and dimensions, restores
  `ts_created` and segments, and invalidates the days for both sites.

Refused or reported before anything changes: different time zones (archived
days would land shifted on T's calendar), different currencies, ambiguous goal
or dimension names, and the conflicts above.

## Install

```
pip install git+https://github.com/Jwrede/matomo-site-merge
```

Python 3.10+, one dependency (PyMySQL). It needs network access to Matomo's
MySQL/MariaDB database and, for `--console`, a way to run Matomo's console.

## Example

Take a database backup first. Then:

```
export MATOMO_DB_PASSWORD=...
matomo-site-merge plan --matomo-config /var/www/matomo/config/config.ini.php --source 1 --target 2
matomo-site-merge apply --matomo-config /var/www/matomo/config/config.ini.php --source 1 --target 2 \
    --console "php /var/www/matomo/console" --yes
php /var/www/matomo/console core:archive
matomo-site-merge verify --matomo-config /var/www/matomo/config/config.ini.php \
    --journal matomo-merge-1-into-2-20261124-063812.json
```

`--matomo-config` reads host, user, database and table prefix from Matomo's
config file; `--host`, `--port`, `--user`, `--database`, `--prefix` override
it, and the password comes from `MATOMO_DB_PASSWORD` or the config file.

Output of the end-to-end test "relaunch with purged history" (site 1 has
January 1 to 15 only as archives and raw logs for January 20 to 25; site 2 has
raw logs from January 22 to February 5; both have a goal "Signup"; the console
path and the journal name are shortened):

```
$ matomo-site-merge apply ... --source 1 --target 2 --yes
merge site 1 'Old site' into site 2 'New site'
  raw logs:  12 visits of S on 6 days move to T and are re-archived
  archives:  17 archived periods of S move as they are (15 days with 45 visits whose raw logs are gone)
  invalidate for T: day x6, month x1, week x2, year x1
  goal 1 -> 1
  note: 21 days change for T (2024-01-01..2024-01-25)
journal: matomo-merge-1-into-2-20261124-065012.json
created in T: 0 goals, 0 custom dimensions
moved 12 visits
moved 17 archived periods
$ php console core:invalidate-report-data ...
$ php console core:invalidate-report-data ...
$ php console core:invalidate-report-data ...
$ php console core:invalidate-report-data ...
$ php console cache:clear
invalidated; run core:archive (or let the cron do it), then `verify`
site 1 now has no raw data; delete it in Matomo once verify is clean

(core:archive, then verify:)
verify: 34 of 34 days and months match
```

## Tests

* Unit tests for the planner and the goal and dimension mapping
  (`pytest tests --ignore=tests/e2e`).
* End-to-end tests against a real Matomo: `e2e/up.sh` starts MariaDB and
  Matomo in Docker and clicks through the web installer; `MSM_E2E=1 pytest
  tests/e2e` then tracks visits and conversions through `matomo.php`,
  archives with `core:archive`, deletes raw logs the way log purging does,
  merges, re-archives, and reads the results back through Matomo's reporting
  API (`VisitsSummary.get`, `Goals.get`, `CustomDimensions.getCustomDimension`),
  not through the tool's own queries. Scenarios: relaunch with purged history
  (days, weeks, the month across both sites, the year, goal conversions);
  goal and custom dimension renumbering followed by `undo`; an archive-only
  day that conflicts with target data.
* CI runs them on Matomo 5 with MariaDB 11.4 and MySQL 8.4, and on Matomo 4
  with MariaDB 10.11, weekly as well.

## Limits

* Same Matomo installation only (both sites in one database). For two
  installations, move the site first with Matomo's Migration plugin.
* Reports of premium plugins (Funnels, Heatmaps, Form Analytics, Media
  Analytics and others) and their own tables are not handled. Their archives
  of moved periods move along, but their raw tables are not touched.
* Archived reports cannot be added up. Days where both sites have data and S
  has no raw logs are conflicts, not merged.
* Unique visitors of a day or period are recomputed from raw logs where they
  exist; for archive-only days they stay what S had.
* The Google Analytics importer keeps per-site settings (imported date
  ranges) that are not moved; I have not tested the tool with data imported
  from Google Analytics, only with archives whose raw logs were deleted.
* `verify` checks visits per day and month, not every report.
* `undo` cannot bring back T's own empty archives of moved periods (they are
  deleted when S's archive takes their place) and leaves orphan log rows
  without a visit in T.
* Take a database backup before `apply`. The journal is a safety net, not a
  backup.

## License

MIT
