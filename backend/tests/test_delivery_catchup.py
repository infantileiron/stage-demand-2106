"""Local SQLite/TestClient checks for per-day delivery catch-up.

Run from backend/: DATABASE_URL=sqlite:////tmp/stage-test.sqlite3 pytest -q tests/test_delivery_catchup.py
The real worker function is called directly; only the Redis queue transport is absent.
"""
import csv
import io
import os
import threading
from datetime import datetime, timedelta, timezone

os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/stage-delivery-test.sqlite3")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("SEED_DEMO", "false")

from fastapi.testclient import TestClient
from src.main import app
from src.utils.database import init_db, insert_row, list_rows, update_row
from src.services import schedule as delivery
from src.services.forecast import analyze

SHANGHAI = delivery.SHANGHAI


def reset():
    if os.path.exists("/tmp/stage-delivery-test.sqlite3"):
        os.remove("/tmp/stage-delivery-test.sqlite3")
    init_db()


def days(start, count, base=100):
    return [{"date": (start + timedelta(days=i)).isoformat(), "prediction": base + i,
        "lower": base + i - 10, "upper": base + i + 5, "ushers": max(2, (base + i) // 20), "event": 0}
        for i in range(count)]


def make_report(title, venue, start, created_at, status="approved", count=3):
    """A report whose forecast horizon starts at ``start`` and whose known time is ``created_at``."""
    run = insert_row("runs", title + "-run", {"scenario_id": 1, "status": "completed", "error": None,
        "result": {"capacity": 800, "venue": venue, "forecast": []}})
    plan = insert_row("plans", title + "-plan", {"name": title + "-plan", "run_id": run["id"],
        "venue": venue, "days": [], "status": "approved", "approved_by": "admin@stage.local"})
    report = insert_row("reports", title, {"title": title, "plan_id": plan["id"], "venue": venue,
        "days": days(start, count), "status": status, "approved_at": created_at.isoformat()}, preserve=True)
    return report


def make_schedule(name, next_run, daily_time="08:30", source_report_id=None, enabled=True):
    return insert_row("schedules", name, {"name": name, "source_report_id": source_report_id,
        "daily_time": daily_time, "enabled": enabled, "next_run": next_run.isoformat(),
        "last_run": None, "last_error": None}, preserve=True)


def at(day, hour=9):
    return datetime(day.year, day.month, day.day, hour, 0, tzinfo=SHANGHAI)


def snapshot_dates(schedule_id):
    return sorted(s["business_date"] for s in list_rows("delivery_snapshots")
        if s["schedule_id"] == schedule_id)


def test_gap_across_multiple_days_creates_one_snapshot_per_day():
    reset()
    start = datetime(2026, 11, 1, tzinfo=SHANGHAI)
    report = make_report("r1", "大剧场", start.date(), at(datetime(2026, 10, 30)))
    schedule = make_schedule("plan-gap", at(datetime(2026, 11, 1)), source_report_id=None)
    now = at(datetime(2026, 11, 5))
    delivered = delivery.run_schedule(schedule, now)
    assert snapshot_dates(schedule["id"]) == ["2026-11-01", "2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05"]
    assert len(delivered) == 5
    stored = update_row("schedules", schedule["id"], {})
    assert stored["next_run"].startswith("2026-11-06")
    assert stored["last_business_date"] == "2026-11-05"


def test_each_day_uses_latest_report_known_before_that_cutoff():
    reset()
    early = make_report("early", "大剧场", datetime(2026, 11, 1).date(), at(datetime(2026, 11, 1), hour=7))
    late = make_report("late", "大剧场", datetime(2026, 11, 3).date(), at(datetime(2026, 11, 3), hour=7))
    schedule = make_schedule("plan-cutoff", at(datetime(2026, 11, 1)))
    delivery.run_schedule(schedule, at(datetime(2026, 11, 4)))
    snaps = {s["business_date"]: s for s in list_rows("delivery_snapshots") if s["schedule_id"] == schedule["id"]}
    assert snaps["2026-11-01"]["sources"][0]["id"] == early["id"]
    assert snaps["2026-11-02"]["sources"][0]["id"] == early["id"]
    # The late report was approved on 11-03 at 07:00, before the 08:30 cutoff.
    assert snaps["2026-11-03"]["sources"][0]["id"] == late["id"]
    assert snaps["2026-11-04"]["sources"][0]["id"] == late["id"]


def test_report_approved_after_cutoff_is_not_used_for_that_day():
    reset()
    report = make_report("future", "大剧场", datetime(2026, 11, 1).date(), at(datetime(2026, 11, 2), hour=7))
    schedule = make_schedule("plan-future", at(datetime(2026, 11, 1)))
    delivery.run_schedule(schedule, at(datetime(2026, 11, 2), hour=9))
    # 11-01 cutoff 08:30 precedes approval; blocked there and nothing later is produced.
    assert snapshot_dates(schedule["id"]) == []
    stored = update_row("schedules", schedule["id"], {})
    assert stored["blocked_date"] == "2026-11-01"
    assert stored["next_run"].startswith("2026-11-01")


def test_fixed_source_uses_the_named_report_and_does_not_take_a_later_one():
    reset()
    fixed = make_report("fixed", "大剧场", datetime(2026, 11, 1).date(), at(datetime(2026, 11, 1), hour=5))
    later = make_report("later", "大剧场", datetime(2026, 11, 1).date(), at(datetime(2026, 11, 2), hour=5))
    schedule = make_schedule("plan-fixed", at(datetime(2026, 11, 1)), source_report_id=fixed["id"])
    delivery.run_schedule(schedule, at(datetime(2026, 11, 2)))
    for snapshot in list_rows("delivery_snapshots"):
        if snapshot["schedule_id"] == schedule["id"]:
            assert snapshot["sources"][0]["id"] == fixed["id"]
    assert snapshot_dates(schedule["id"]) == ["2026-11-01", "2026-11-02"]


def test_fixed_source_approved_later_blocks_until_cutoff():
    reset()
    fixed = make_report("fixed-later", "大剧场", datetime(2026, 11, 1).date(), at(datetime(2026, 11, 2), hour=7))
    schedule = make_schedule("plan-fixed-late", at(datetime(2026, 11, 1)), source_report_id=fixed["id"])
    delivery.run_schedule(schedule, at(datetime(2026, 11, 2), hour=9))
    assert snapshot_dates(schedule["id"]) == []
    stored = update_row("schedules", schedule["id"], {})
    assert stored["blocked_date"] == "2026-11-01"
    assert "之后才确认" in stored["blocked_reason"]
    # The fixed source cannot be pulled early: still blocked after the cutoff has passed.
    delivery.run_schedule(stored, at(datetime(2026, 11, 3)))
    assert snapshot_dates(schedule["id"]) == []
    assert update_row("schedules", schedule["id"], {})["blocked_reason"].startswith("固定来源纪要")


def test_no_available_reports_blocks_at_that_day_and_does_not_skip():
    reset()
    schedule = make_schedule("plan-empty", at(datetime(2026, 11, 1)))
    delivery.run_schedule(schedule, at(datetime(2026, 11, 4)))
    stored = update_row("schedules", schedule["id"], {})
    assert snapshot_dates(schedule["id"]) == []
    assert stored["blocked_date"] == "2026-11-01"
    assert stored["next_run"].startswith("2026-11-01")
    assert "没有已确认纪要" in stored["blocked_reason"]
    # A report approved after later cutoffs still cannot claim the blocked day: still 11-01.
    make_report("too-late", "大剧场", datetime(2026, 11, 1).date(), at(datetime(2026, 11, 5), hour=9))
    delivery.run_schedule(stored, at(datetime(2026, 11, 5), hour=9))
    assert snapshot_dates(schedule["id"]) == []
    assert update_row("schedules", schedule["id"], {})["blocked_date"] == "2026-11-01"
    # A report known before the blocked cutoff resumes at 11-01 and fills the gap.
    make_report("available", "大剧场", datetime(2026, 11, 1).date(), at(datetime(2026, 11, 1), hour=7))
    delivery.run_schedule(update_row("schedules", schedule["id"], {}), at(datetime(2026, 11, 5), hour=9))
    assert snapshot_dates(schedule["id"]) == ["2026-11-01", "2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05"]
    assert not update_row("schedules", schedule["id"], {}).get("blocked_date")


def test_interrupted_after_snapshot_write_resumes_without_duplicate_or_overwrite():
    reset()
    report = make_report("resume", "大剧场", datetime(2026, 11, 1).date(), at(datetime(2026, 10, 30)))
    schedule = make_schedule("plan-resume", at(datetime(2026, 11, 1)))
    # Simulate a crash after the snapshot persisted but before progress advanced.
    sources = delivery.source_reports(schedule, datetime(2026, 11, 1, 8, 30, tzinfo=SHANGHAI))
    key = delivery.snapshot_key(schedule["id"], "2026-11-01")
    delivery.insert_if_absent("delivery_snapshots", key,
        {"schedule_id": schedule["id"], "business_date": "2026-11-01", "delivered_at": "crashed",
         "rows": delivery.pivot_rows(sources), "sources": []})
    before = next(s for s in list_rows("delivery_snapshots") if s["key"] == key)
    delivery.run_schedule(schedule, at(datetime(2026, 11, 3)))
    after = next(s for s in list_rows("delivery_snapshots") if s["key"] == key)
    assert before["id"] == after["id"]
    assert len([s for s in list_rows("delivery_snapshots") if s["key"] == key]) == 1
    assert after["delivered_at"] == "crashed"  # existing snapshot was preserved
    assert snapshot_dates(schedule["id"]) == ["2026-11-01", "2026-11-02", "2026-11-03"]
    state = delivery.delivery_state(update_row("schedules", schedule["id"], {}))
    assert state["progress"]["last_delivered_date"] == "2026-11-03"
    assert state["progress"]["next_due_date"] == "2026-11-04"


def test_concurrent_catchup_does_not_duplicate_or_overwrite():
    reset()
    report = make_report("concurrent", "大剧场", datetime(2026, 11, 1).date(), at(datetime(2026, 10, 30)))
    schedule = make_schedule("plan-concurrent", at(datetime(2026, 11, 1)))
    errors = []

    def worker():
        try:
            delivery.run_schedule(get_row_schedule(schedule["id"]), at(datetime(2026, 11, 5)))
        except Exception as exc:  # pragma: no cover - surfaced in assertion below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert not errors
    keys = [s["key"] for s in list_rows("delivery_snapshots") if s["schedule_id"] == schedule["id"]]
    assert len(keys) == len(set(keys)) == 5
    assert snapshot_dates(schedule["id"]) == ["2026-11-%02d" % day for day in range(1, 6)]


def test_progress_matches_persisted_business_dates():
    reset()
    make_report("progress", "大剧场", datetime(2026, 11, 1).date(), at(datetime(2026, 10, 30)))
    schedule = make_schedule("plan-progress", at(datetime(2026, 11, 1)))
    delivery.run_schedule(schedule, at(datetime(2026, 11, 4)))
    state = delivery.delivery_state(update_row("schedules", schedule["id"], {}))
    dates = [item["business_date"] for item in state["deliveries"]]
    assert state["progress"]["last_delivered_date"] == dates[0]
    assert dates == ["2026-11-04", "2026-11-03", "2026-11-02", "2026-11-01"]
    assert state["progress"]["next_due_date"] == "2026-11-05"
    assert state["progress"]["delivered_count"] == len(dates)


def get_row_schedule(ident):
    from src.utils.database import get_row
    return get_row("schedules", ident)


def test_download_matches_day_snapshot():
    reset()
    report = make_report("csv", "大剧场", datetime(2026, 11, 1).date(), at(datetime(2026, 10, 30)))
    schedule = make_schedule("plan-csv", at(datetime(2026, 11, 1)))
    delivery.run_schedule(schedule, at(datetime(2026, 11, 2)))
    snapshot = delivery.find_snapshot(schedule["id"], "2026-11-01")
    text = delivery.download(schedule["id"], "2026-11-01")
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    assert rows[0][1:] == ["大剧场 / 预测人数", "大剧场 / 预测上座率(%)", "大剧场 / 引导员建议"]
    by_date = {row["date"]: row for row in snapshot["rows"]}
    for row in rows[1:]:
        stored = by_date[row[0]]
        assert row[1:] == [str(stored["prediction"]), str(stored["occupancy_rate"]), str(stored["ushers"])]


def test_occupancy_rate_and_ushers_per_venue_day():
    reset()
    report = make_report("metrics", "大剧场", datetime(2026, 11, 1).date(), at(datetime(2026, 10, 30)))
    schedule = make_schedule("plan-metrics", at(datetime(2026, 11, 1)))
    delivery.run_schedule(schedule, at(datetime(2026, 11, 1)))
    snapshot = delivery.find_snapshot(schedule["id"], "2026-11-01")
    day = next(r for r in snapshot["rows"] if r["date"] == "2026-11-01")
    assert day["occupancy_rate"] == round(day["prediction"] / 800 * 100, 2)
    assert day["ushers"] >= 2


def test_api_catch_up_and_download():
    reset()
    start = datetime(2026, 11, 1).date()
    make_report("api-report", "大剧场", start, at(datetime(2026, 10, 30)))
    client = TestClient(app)
    token = client.post("/auth/login", json={"email": "admin@stage.local", "password": "Stage2611!"})
    assert token.status_code == 401  # no seed users in this reset
    # Insert one admin directly for this isolated API check.
    from src.utils.security import password_hash
    insert_row("users", "admin@stage.local", {"email": "admin@stage.local", "name": "主管", "role": "admin",
        "password_hash": password_hash("Stage2611!")}, preserve=True)
    token = client.post("/auth/login", json={"email": "admin@stage.local", "password": "Stage2611!"})
    assert token.status_code == 200
    headers = {"Authorization": "Bearer " + token.json()["access_token"]}
    schedule = make_schedule("plan-api", at(datetime(2026, 11, 1)))
    response = client.post("/schedules/%d/catch-up" % schedule["id"], headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    now = delivery.clock()
    expected_last = now.date().isoformat() if now.hour > 8 or (now.hour == 8 and now.minute >= 30) \
        else (now.date() - timedelta(days=1)).isoformat()
    assert body["schedule"]["progress"]["last_delivered_date"] == expected_last
    assert [item["business_date"] for item in body["schedule"]["deliveries"]][0] == expected_last
    assert body["schedule"]["progress"]["next_due_date"] > expected_last
    download = client.get("/schedules/%d/deliveries/2026-11-01/download" % schedule["id"], headers=headers)
    assert download.status_code == 200
    assert "大剧场 / 预测上座率(%)" in download.text
    blocked = client.post("/schedules/%d/catch-up" % schedule["id"], headers=headers)
    assert blocked.status_code == 200
    assert blocked.json()["delivered_ids"] == []  # idempotent: nothing due is rewritten
