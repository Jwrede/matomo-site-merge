"""End-to-end against a real Matomo (e2e/up.sh). Results are read back through
Matomo's own reporting API, not through the tool's database queries.

    MSM_E2E=1 pytest tests/e2e -v
"""
import datetime as dt
import hashlib
import json
import os
import secrets
import subprocess
import urllib.error
import urllib.parse
import urllib.request

import pymysql
import pymysql.cursors
import pytest

from matomo_site_merge.cli import main

pytestmark = pytest.mark.skipif(os.environ.get("MSM_E2E") != "1", reason="needs e2e/up.sh")

URL = os.environ.get("MSM_URL", "http://127.0.0.1:18080")
CONTAINER = os.environ.get("MSM_CONTAINER", "msm-matomo")
CONSOLE = f"docker exec {CONTAINER} php /var/www/html/console"
DB = ["--host", "127.0.0.1", "--port", "13306", "--user", "matomo", "--database", "matomo"]
os.environ["MATOMO_DB_PASSWORD"] = "matomo"


def conn():
    return pymysql.connect(host="127.0.0.1", port=13306, user="matomo", password="matomo",
                           database="matomo", cursorclass=pymysql.cursors.DictCursor,
                           autocommit=True, init_command="SET time_zone='+00:00'")


def sql(q, args=()):
    with conn() as c, c.cursor() as cur:
        cur.execute(q, args)
        return cur.fetchall()


@pytest.fixture(scope="session")
def token():
    salt = subprocess.run(["docker", "exec", CONTAINER, "sh", "-c",
                           "grep '^salt' /var/www/html/config/config.ini.php"],
                          capture_output=True, text=True, check=True).stdout.split('"')[1]
    tok = secrets.token_hex(16)
    sql("INSERT INTO matomo_user_token_auth (login, description, password, hash_algo, "
        "system_token, date_created, secure_only) VALUES ('admin', 'e2e', %s, 'sha512', 0, "
        "NOW(), 0)", (hashlib.sha512((tok + salt).encode()).hexdigest(),))
    return tok


def api(token, method, **params):
    data = urllib.parse.urlencode({"module": "API", "method": method, "format": "json",
                                   "token_auth": token, **params}).encode()
    try:
        with urllib.request.urlopen(f"{URL}/index.php", data, timeout=600) as r:
            out = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method}: HTTP {e.code}: {e.read()[:500]!r}") from None
    if isinstance(out, dict) and out.get("result") == "error":
        raise RuntimeError(f"{method}: {out['message']}")
    return out


def console(*args):
    r = subprocess.run(CONSOLE.split() + list(args), capture_output=True, text=True, timeout=1800)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout


def new_site(token, name):
    idsite = int(api(token, "SitesManager.addSite", siteName=name,
                     **{"urls[0]": f"https://{name.lower().replace(' ', '-')}.example.org"},
                     timezone="UTC")["value"])
    sql("UPDATE matomo_site SET ts_created='2023-12-01 00:00:00' WHERE idsite=%s", (idsite,))
    return idsite


def add_goal(token, idsite, name):
    return int(api(token, "Goals.addGoal", idSite=idsite, name=name, matchAttribute="manually",
                   pattern="manual", patternType="contains")["value"])


def add_dim(token, idsite, name):
    return int(api(token, "CustomDimensions.configureNewCustomDimension", idSite=idsite,
                   name=name, scope="visit", active=1)["value"])


def track(token, idsite, days, per_day, goal=None, dims=None):
    """per_day visits on each day at 12:00 UTC, each with one conversion if goal."""
    reqs = []
    for d in days:
        for i in range(per_day):
            vid = secrets.token_hex(8)
            t = dt.datetime.combine(d, dt.time(12, 0)) + dt.timedelta(minutes=i)
            base = {"idsite": idsite, "rec": 1, "_id": vid, "cid": vid,
                    "url": f"https://x.example.org/p{i}", "cdt": t.strftime("%Y-%m-%d %H:%M:%S")}
            for k, v in (dims or {}).items():
                base[f"dimension{k}"] = v
            reqs.append("?" + urllib.parse.urlencode(base))
            if goal:
                c = {**base, "idgoal": goal,
                     "cdt": (t + dt.timedelta(seconds=30)).strftime("%Y-%m-%d %H:%M:%S")}
                reqs.append("?" + urllib.parse.urlencode(c))
    for i in range(0, len(reqs), 200):
        body = json.dumps({"requests": reqs[i:i + 200], "token_auth": token}).encode()
        req = urllib.request.Request(f"{URL}/matomo.php", body,
                                     {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=600) as r:
            assert json.loads(r.read())["status"] == "success"


def archive(*sites):
    console("core:archive", f"--url={URL}/", "--force-idsites=" + ",".join(map(str, sites)),
            "--force-date-range=2024-01-01,2024-02-29", "--force-periods=day,week,month,year")


def process_invalidations(*sites):
    console("core:archive", f"--url={URL}/", "--force-idsites=" + ",".join(map(str, sites)))


def visits(token, idsite, period, date):
    r = api(token, "VisitsSummary.get", idSite=idsite, period=period, date=date)
    if isinstance(r, list):
        return 0
    return int(r.get("nb_visits", 0))


def by_day(token, idsite, start, end):
    r = api(token, "VisitsSummary.get", idSite=idsite, period="day", date=f"{start},{end}")
    return {k: (int(v["nb_visits"]) if isinstance(v, dict) and v else 0) for k, v in r.items()}


def conversions(token, idsite, idgoal, period, date):
    r = api(token, "Goals.get", idSite=idsite, idGoal=idgoal, period=period, date=date)
    return int(r.get("nb_conversions", 0)) if isinstance(r, dict) else 0


def days(a, b):
    d = a
    while d <= b:
        yield d
        d += dt.timedelta(days=1)


D = dt.date


def test_relaunch_with_purged_history(token, tmp_path, capsys):
    """Old site: Jan 1-15 only as archives (raw logs deleted), raw logs Jan 20-25.
    New site: raw logs Jan 22 - Feb 5. Same goal 'Signup' with ID 1 on both."""
    S = 1
    sql("UPDATE matomo_site SET ts_created='2023-12-01 00:00:00' WHERE idsite=1")
    T = new_site(token, "New site")
    assert add_goal(token, S, "Signup") == 1
    assert add_goal(token, T, "Signup") == 1
    track(token, S, days(D(2024, 1, 1), D(2024, 1, 15)), 3, goal=1)
    archive(S)
    # Purge the raw logs of Jan 1-15, as Matomo's "delete old raw data" would.
    old = [r["idvisit"] for r in sql("SELECT idvisit FROM matomo_log_visit WHERE idsite=%s", (S,))]
    for t in ("log_link_visit_action", "log_conversion", "log_visit"):
        sql(f"DELETE FROM matomo_{t} WHERE idvisit IN %s", (tuple(old),))
    track(token, S, days(D(2024, 1, 20), D(2024, 1, 25)), 2, goal=1)
    track(token, T, days(D(2024, 1, 22), D(2024, 2, 5)), 4, goal=1)
    archive(S, T)

    s_days = by_day(token, S, "2024-01-01", "2024-02-29")
    t_days = by_day(token, T, "2024-01-01", "2024-02-29")
    assert s_days["2024-01-03"] == 3 and s_days["2024-01-21"] == 2
    s_jan, t_jan = visits(token, S, "month", "2024-01-10"), visits(token, T, "month", "2024-01-10")
    t_feb = visits(token, T, "month", "2024-02-10")
    s_conv = conversions(token, S, 1, "month", "2024-01-10")
    t_conv = conversions(token, T, 1, "month", "2024-01-10")
    assert s_jan == 45 + 12 and s_conv == 57

    assert main(["plan", *DB, "--source", str(S), "--target", str(T)]) == 0
    out = capsys.readouterr().out
    assert "15 days with 45 visits whose raw logs are gone" in out

    journal = tmp_path / "j.json"
    assert main(["apply", *DB, "--source", str(S), "--target", str(T), "--yes",
                 "--journal", str(journal), "--console", CONSOLE]) == 0
    process_invalidations(T)

    after = by_day(token, T, "2024-01-01", "2024-02-29")
    for d in s_days:
        assert after[d] == s_days[d] + t_days[d], d
    assert visits(token, T, "month", "2024-01-10") == s_jan + t_jan
    assert visits(token, T, "month", "2024-02-10") == t_feb
    assert visits(token, T, "year", "2024-06-01") == s_jan + t_jan + t_feb
    assert visits(token, T, "week", "2024-01-03") == 3 * 7  # Mon Jan 1 - Sun Jan 7, moved as is
    assert conversions(token, T, 1, "month", "2024-01-10") == s_conv + t_conv
    assert main(["verify", *DB, "--journal", str(journal)]) == 0
    assert sql("SELECT COUNT(*) AS n FROM matomo_log_visit WHERE idsite=%s", (S,))[0]["n"] == 0


def test_goals_and_dimensions_are_renumbered_and_undo_restores(token, tmp_path):
    S = new_site(token, "Shop A")
    T = new_site(token, "Shop B")
    assert add_goal(token, S, "Newsletter") == 1
    assert add_goal(token, T, "Purchase") == 1
    assert add_goal(token, T, "Newsletter") == 2
    s_dim = add_dim(token, S, "Plan")
    t_dim = add_dim(token, T, "Tier")
    track(token, S, days(D(2024, 2, 1), D(2024, 2, 3)), 2, goal=1, dims={s_dim: "pro"})
    track(token, T, days(D(2024, 2, 2), D(2024, 2, 4)), 3, goal=1, dims={t_dim: "gold"})
    archive(S, T)
    assert conversions(token, T, 1, "month", "2024-02-10") == 9
    s_month = visits(token, S, "month", "2024-02-10")

    journal = tmp_path / "j.json"
    assert main(["apply", *DB, "--source", str(S), "--target", str(T), "--yes",
                 "--journal", str(journal), "--console", CONSOLE]) == 0
    process_invalidations(T)

    assert visits(token, T, "month", "2024-02-10") == 6 + 9
    assert conversions(token, T, 1, "month", "2024-02-10") == 9  # Purchase unchanged
    assert conversions(token, T, 2, "month", "2024-02-10") == 6  # Newsletter of S
    dims = api(token, "CustomDimensions.getConfiguredCustomDimensions", idSite=T)
    plan_dim = [d for d in dims if d["name"] == "Plan"][0]
    rows = api(token, "CustomDimensions.getCustomDimension", idSite=T, period="month",
               date="2024-02-10", idDimension=plan_dim["idcustomdimension"])
    assert {r["label"]: int(r["nb_visits"]) for r in rows}.get("pro") == 6
    rows = api(token, "CustomDimensions.getCustomDimension", idSite=T, period="month",
               date="2024-02-10", idDimension=t_dim)
    assert {r["label"]: int(r["nb_visits"]) for r in rows}.get("gold") == 9
    assert main(["verify", *DB, "--journal", str(journal)]) == 0

    assert main(["undo", *DB, "--journal", str(journal), "--console", CONSOLE]) == 0
    process_invalidations(S, T)
    assert visits(token, S, "month", "2024-02-10") == s_month
    assert visits(token, T, "month", "2024-02-10") == 9
    assert conversions(token, S, 1, "month", "2024-02-10") == 6
    rows = api(token, "CustomDimensions.getCustomDimension", idSite=S, period="month",
               date="2024-02-10", idDimension=s_dim)
    assert {r["label"]: int(r["nb_visits"]) for r in rows}.get("pro") == 6


def test_archive_only_day_with_target_data_conflicts(token, tmp_path, capsys):
    S = new_site(token, "Conflict A")
    T = new_site(token, "Conflict B")
    track(token, S, [D(2024, 1, 10)], 2)
    archive(S)
    sql("DELETE FROM matomo_log_link_visit_action WHERE idsite=%s", (S,))
    sql("DELETE FROM matomo_log_visit WHERE idsite=%s", (S,))
    track(token, T, [D(2024, 1, 10)], 1)
    archive(T)
    assert main(["plan", *DB, "--source", str(S), "--target", str(T)]) == 1
    assert "CONFLICT day 2024-01-10" in capsys.readouterr().out
    assert main(["apply", *DB, "--source", str(S), "--target", str(T), "--yes",
                 "--journal", str(tmp_path / "j.json")]) == 1
    assert not (tmp_path / "j.json").exists()
    assert main(["apply", *DB, "--source", str(S), "--target", str(T), "--yes",
                 "--on-conflict", "keep-target", "--journal", str(tmp_path / "k.json"),
                 "--console", CONSOLE]) == 0
    process_invalidations(T)
    assert visits(token, T, "day", "2024-01-10") == 1
