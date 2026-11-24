from matomo_site_merge.mapping import Dimension, Goal, column_moves, map_dims, map_goals
from matomo_site_merge.cli import read_matomo_config
from matomo_site_merge.inventory import tzinfo
import datetime as dt


def test_goals_match_by_name_and_new_ones_keep_their_id_when_free():
    src = [Goal(1, "Signup"), Goal(4, "Download")]
    tgt = [Goal(1, "Purchase"), Goal(2, " signup ")]
    m, create, err = map_goals(src, tgt, {}, False)
    assert err == []
    assert m == {1: 2, 4: 4}
    assert [g.idgoal for g in create] == [4]


def test_goal_with_taken_id_gets_next_free_id():
    m, create, err = map_goals([Goal(1, "Newsletter")], [Goal(1, "Purchase")], {}, False)
    assert m == {1: 2} and create[0].idgoal == 2 and err == []


def test_goal_override_and_ambiguity():
    tgt = [Goal(1, "Signup"), Goal(2, "Signup")]
    _, _, err = map_goals([Goal(3, "Signup")], tgt, {}, False)
    assert "several goals" in err[0]
    m, _, err = map_goals([Goal(3, "Signup")], tgt, {3: 2}, False)
    assert m == {3: 2} and err == []


def test_dimensions_go_to_free_slots_and_columns_move():
    src = [Dimension(1, "Plan", "visit", 1), Dimension(2, "Author", "action", 1)]
    tgt = [Dimension(1, "Country tier", "visit", 1), Dimension(2, "author", "action", 3)]
    m, create, err = map_dims(src, tgt, {}, False, {"visit": [2, 3], "action": [1, 2]})
    assert err == []
    assert m == {1: 3, 2: 2}
    assert create == [Dimension(3, "Plan", "visit", 2)]
    assert column_moves(src, tgt + create, m) == {"visit": {1: 2}, "action": {1: 3}}


def test_read_config(tmp_path):
    p = tmp_path / "config.ini.php"
    p.write_text('; <?php exit; ?> DO NOT REMOVE THIS LINE\n[database]\nhost = "db"\n'
                 'username = "matomo"\npassword = "pw"\ndbname = "m"\ntables_prefix = "x_"\n'
                 '[General]\nsalt = "s"\n')
    c = read_matomo_config(str(p))
    assert c["host"] == "db" and c["tables_prefix"] == "x_" and "salt" not in c


def test_manual_offset_time_zones():
    assert tzinfo("UTC+5.5").utcoffset(None) == dt.timedelta(hours=5, minutes=30)
    assert tzinfo("UTC-3").utcoffset(None) == dt.timedelta(hours=-3)
    assert tzinfo("Europe/Berlin").utcoffset(dt.datetime(2024, 7, 1)) == dt.timedelta(hours=2)
