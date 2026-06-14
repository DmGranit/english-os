"""B-enc1: режим «Урок» — day_map['new'] критерий по урок-завершению."""
import db

UID = 1


def test_day_map_new_false_initially(fresh_db):
    assert fresh_db.day_map(UID)["new"] is False


def test_day_map_new_true_after_lesson_session(fresh_db):
    fresh_db.log_session(UID, "new")
    assert fresh_db.day_map(UID)["new"] is True


def test_day_map_new_true_after_lesson_mode_session(fresh_db):
    fresh_db.log_session(UID, "lesson")
    assert fresh_db.day_map(UID)["new"] is True


def test_day_map_new_false_for_flow_session(fresh_db):
    fresh_db.log_session(UID, "flow")
    assert fresh_db.day_map(UID)["new"] is False


def test_day_map_new_false_for_scenario_session(fresh_db):
    fresh_db.log_session(UID, "scenario")
    assert fresh_db.day_map(UID)["new"] is False


def test_day_map_new_true_for_direct_learning(fresh_db):
    # прямое введение слов (учим X, темы) всё ещё закрывает слот
    fresh_db.start_learning([1], UID)   # via='direct' по умолчанию
    assert fresh_db.day_map(UID)["new"] is True


def test_day_map_new_false_promote_without_lesson(fresh_db):
    # promote_new вводит слова, но слот NEW не закрыт до завершения урока
    fresh_db.promote_new(UID, n=1)
    assert fresh_db.day_map(UID)["new"] is False


def test_day_map_new_true_promote_then_finish_lesson(fresh_db):
    fresh_db.promote_new(UID, n=1)
    fresh_db.log_session(UID, "new")   # _finish_lesson вызывает log_session('new')
    assert fresh_db.day_map(UID)["new"] is True


def test_day_map_new_scenario_via_still_false(fresh_db):
    # scenario-via не закрывает NEW слот (A1.3 не ломается)
    fresh_db.start_learning([1], UID, via="scenario")
    assert fresh_db.day_map(UID)["new"] is False
