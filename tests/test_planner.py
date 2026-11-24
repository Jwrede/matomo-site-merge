import datetime as dt

from matomo_site_merge.planner import DAY, MONTH, WEEK, Period, SiteData, make_plan

D = dt.date


def day(d, n):
    return Period(DAY, d, d), n


def test_archive_only_days_move_when_target_is_empty():
    src = SiteData(archives=dict([day(D(2024, 1, 1), 5), day(D(2024, 1, 2), 3)]))
    tgt = SiteData(raw_visits={D(2024, 1, 20): 4})
    plan = make_plan(src, tgt)
    assert plan.ok
    assert plan.move_archives == [Period(DAY, D(2024, 1, 1), D(2024, 1, 1)),
                                  Period(DAY, D(2024, 1, 2), D(2024, 1, 2))]
    assert ("month", D(2024, 1, 1)) in plan.invalidate
    assert ("week", D(2024, 1, 1)) in plan.invalidate
    assert not any(n == "day" for n, _ in plan.invalidate)


def test_raw_days_are_invalidated_not_moved_as_archives():
    src = SiteData(raw_visits={D(2024, 1, 5): 2},
                   archives=dict([day(D(2024, 1, 5), 2)]))
    plan = make_plan(src, SiteData())
    assert plan.move_raw_days == [D(2024, 1, 5)]
    assert plan.move_archives == []
    assert plan.invalidate == [("day", D(2024, 1, 5))]


def test_archive_only_day_with_target_data_is_a_conflict():
    src = SiteData(archives=dict([day(D(2024, 1, 5), 2)]))
    tgt = SiteData(raw_visits={D(2024, 1, 5): 7})
    plan = make_plan(src, tgt)
    assert not plan.ok
    assert plan.conflicts[0].period.date1 == D(2024, 1, 5)
    resolved = make_plan(src, tgt, "keep-target")
    assert resolved.ok and resolved.move_archives == []


def test_whole_month_without_target_data_moves_including_month_archive():
    jan = Period(MONTH, D(2024, 1, 1), D(2024, 1, 31))
    src = SiteData(archives=dict([day(D(2024, 1, 3), 4), (jan, 4)]))
    tgt = SiteData(raw_visits={D(2024, 2, 10): 1})
    plan = make_plan(src, tgt)
    assert jan in plan.move_archives
    assert ("month", D(2024, 1, 3)) not in plan.invalidate


def test_month_archive_with_more_visits_than_its_days_conflicts_when_mixed():
    jan = Period(MONTH, D(2024, 1, 1), D(2024, 1, 31))
    # S kept the month but its day archives were purged; T has January data.
    src = SiteData(archives={jan: 40})
    tgt = SiteData(raw_visits={D(2024, 1, 25): 3})
    plan = make_plan(src, tgt)
    assert not plan.ok
    assert "lose 40 visits" in plan.conflicts[0].reason


def test_week_spanning_both_sites_is_rebuilt():
    wk = Period(WEEK, D(2024, 1, 29), D(2024, 2, 4))
    src = SiteData(archives=dict([day(D(2024, 1, 30), 2), (wk, 2)]))
    tgt = SiteData(raw_visits={D(2024, 2, 2): 5})
    plan = make_plan(src, tgt)
    assert wk not in plan.move_archives
    assert ("week", D(2024, 1, 29)) in plan.invalidate
    assert ("month", D(2024, 1, 1)) in plan.invalidate


def test_parents_are_listed_once():
    src = SiteData(archives=dict([day(D(2024, 1, d), 1) for d in range(1, 16)]))
    plan = make_plan(src, SiteData(raw_visits={D(2024, 1, 20): 1}))
    assert [x for x in plan.invalidate if x[0] == "month"] == [("month", D(2024, 1, 1))]
    assert [x for x in plan.invalidate if x[0] == "year"] == [("year", D(2024, 1, 1))]
