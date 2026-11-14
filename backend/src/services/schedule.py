"""Daily station-local delivery. No emails, external webhooks or recipients.

Every due occurrence is materialised as its own snapshot (business date =
scheduled due date). A restart therefore resumes from the first date that has
not been persisted yet, and a snapshot is never rewritten or skipped silently.
"""
import csv
import io
import logging
from datetime import datetime, time, timedelta, timezone
from fastapi import HTTPException
from src.models.schedule import DeliverySchedule
from src.utils.database import find_by_key, list_rows, get_row, insert_if_absent, update_row, update_row_locked

SHANGHAI = timezone(timedelta(hours=8))
MAX_CATCH_UP_DAYS = 10000


class DeliveryBlocked(Exception):
    """The current business date cannot be delivered; catch-up stops there."""


def clock():
    return datetime.now(SHANGHAI)


def delivery_time(daily_time):
    hour, minute = map(int, daily_time.split(":"))
    return time(hour, minute)


def occurrence_on(business_date, daily_time):
    return datetime.combine(datetime.fromisoformat(business_date).date(), delivery_time(daily_time), SHANGHAI)


def next_occurrence(daily_time, now):
    now = now.astimezone(SHANGHAI)
    candidate = datetime.combine(now.date(), delivery_time(daily_time), SHANGHAI)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate.isoformat()


def occurrence_after(business_date, daily_time):
    return occurrence_on(business_date, daily_time) + timedelta(days=1)


def known_at(report):
    """When the report became usable. Explicit approval time wins over row creation."""
    raw = report.get("approved_at") or report.get("created_at")
    if not raw:
        return datetime.min.replace(tzinfo=SHANGHAI)
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        return datetime.min.replace(tzinfo=SHANGHAI)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(SHANGHAI)


def snapshot_key(schedule_id, business_date):
    return str(schedule_id) + ":" + business_date


def find_snapshot(schedule_id, business_date):
    return find_by_key("delivery_snapshots", snapshot_key(schedule_id, business_date))


def source_reports(schedule, cutoff):
    """Reports usable at the cutoff of one business date, never later ones."""
    if schedule["source_report_id"] is not None:
        report = get_row("reports", schedule["source_report_id"])
        if report["status"] != "approved":
            raise DeliveryBlocked("固定来源纪要《" + report["title"] + "》尚未确认")
        if known_at(report) > cutoff:
            raise DeliveryBlocked("固定来源纪要《" + report["title"] + "》在报送时刻之后才确认，"
                + known_at(report).isoformat() + " > 截止 " + cutoff.isoformat())
        return [report]
    selected = {}
    for report in list_rows("reports"):
        if report["status"] != "approved":
            continue
        if known_at(report) > cutoff:
            continue
        venue = report["venue"]
        current = selected.get(venue)
        if current is None or (known_at(report), report["id"]) > (known_at(current), current["id"]):
            selected[venue] = report
    if not selected:
        raise DeliveryBlocked("截止 " + cutoff.isoformat() + " 没有已确认纪要，等待确认后再补报")
    return list(selected.values())


def pivot_rows(reports):
    grouped = {}
    for report in reports:
        plan = get_row("plans", report["plan_id"])
        run = get_row("runs", plan["run_id"])
        capacity = run["result"]["capacity"]
        if capacity <= 0:
            raise DeliveryBlocked("纪要《" + report["title"] + "》所属预测的场馆容量无效")
        for day in report["days"]:
            key = (day["date"], report["venue"])
            if key in grouped:
                raise DeliveryBlocked("同一厅同一天有多份纪要，请先选择唯一来源")
            grouped[key] = {"date": day["date"], "venue": report["venue"], "prediction": day["prediction"],
                "capacity": capacity, "occupancy_rate": round(day["prediction"] / capacity * 100, 2),
                "ushers": day["ushers"]}
    return [grouped[key] for key in sorted(grouped)]


def _advance(schedule, due, now):
    """Move next_run past a persisted business date, never backwards."""
    business_date = due.date().isoformat()
    following = occurrence_after(business_date, schedule["daily_time"]).isoformat()

    def mutate(current):
        current_due = datetime.fromisoformat(current["next_run"]).astimezone(SHANGHAI)
        if current_due > due:
            return {}
        return {"next_run": following, "last_run": now.isoformat(), "last_error": None,
            "last_business_date": business_date, "blocked_date": None, "blocked_reason": None,
            "blocked_at": None}

    update_row_locked("schedules", schedule["id"], mutate)


def _block(schedule, due, reason, now):
    business_date = due.date().isoformat()

    def mutate(current):
        current_due = datetime.fromisoformat(current["next_run"]).astimezone(SHANGHAI)
        if current_due > due:
            return {}
        if (current.get("blocked_date") == business_date
                and current.get("blocked_reason") == reason):
            return {}
        return {"last_error": reason, "blocked_date": business_date, "blocked_reason": reason,
            "blocked_at": now.isoformat()}

    update_row_locked("schedules", schedule["id"], mutate)


def _snapshot_payload(schedule, business_date, cutoff, now, rows, sources):
    return {"schedule_id": schedule["id"], "business_date": business_date,
        "cutoff_at": cutoff.isoformat(), "delivered_at": now.isoformat(), "rows": rows,
        "sources": [{"id": report["id"], "title": report["title"], "venue": report["venue"],
            "known_at": known_at(report).isoformat()} for report in sources],
        "metrics": {"prediction": "日到场人数点预测（人）",
            "occupancy_rate": "预测人数/场馆容量×100%",
            "ushers": "ceil(预测90%上界/每引导员人数)，最少2名"}}


def run_schedule(schedule, now=None):
    """Catch up every due date in order; stop at the first date that cannot be delivered."""
    now = (now or clock()).astimezone(SHANGHAI)
    delivered = []
    for _ in range(MAX_CATCH_UP_DAYS):
        schedule = get_row("schedules", schedule["id"])
        if not schedule["enabled"]:
            break
        due = datetime.fromisoformat(schedule["next_run"]).astimezone(SHANGHAI)
        if due > now:
            break
        business_date = due.date().isoformat()
        existing = find_snapshot(schedule["id"], business_date)
        if existing is not None:
            # A crash may have persisted the row before advancing progress, or
            # another worker may have won the race; resume from what is stored.
            _advance(schedule, due, now)
            if existing.get("cutoff_at") is None:
                # A snapshot written before provenance was recorded still counts as done.
                logging.info("Delivery snapshot %s predates source tracking", existing["key"])
            delivered.append(existing["id"])
            continue
        try:
            sources = source_reports(schedule, due)
            rows = pivot_rows(sources)
        except DeliveryBlocked as exc:
            _block(schedule, due, str(exc), now)
            break
        except Exception as exc:
            logging.exception("Station-local delivery failed")
            _block(schedule, due, "报送暂未完成：" + str(exc)[:300], now)
            break
        snapshot, _ = insert_if_absent("delivery_snapshots", snapshot_key(schedule["id"], business_date),
            _snapshot_payload(schedule, business_date, due, now, rows, sources))
        _advance(schedule, due, now)
        delivered.append(snapshot["id"])
    else:
        logging.warning("Delivery schedule %s hit catch-up safety limit", schedule["id"])
    return delivered


def run_due(now=None):
    now = (now or clock()).astimezone(SHANGHAI)
    delivered = []
    for schedule in list_rows("schedules"):
        if not schedule["enabled"]:
            continue
        try:
            delivered.extend(run_schedule(schedule, now))
        except Exception:
            logging.exception("Station-local delivery failed")
    return delivered


def catch_up(ident):
    schedule = get_row("schedules", ident)
    if not schedule["enabled"]:
        raise HTTPException(409, "计划已暂停，暂停期间不报送；请启用后再补报")
    delivered = run_schedule(schedule, clock())
    return {"schedule": delivery_state(get_row("schedules", ident)), "delivered_ids": delivered}


def delivery_state(schedule):
    snapshots = [snapshot for snapshot in list_rows("delivery_snapshots")
        if snapshot["schedule_id"] == schedule["id"]]
    deliveries = sorted(({"id": snapshot["id"], "business_date": snapshot["business_date"],
        "delivered_at": snapshot["delivered_at"], "row_count": len(snapshot["rows"]),
        "cutoff_at": snapshot.get("cutoff_at"),
        "sources": snapshot.get("sources", [])} for snapshot in snapshots),
        key=lambda item: item["business_date"], reverse=True)
    delivered_dates = {item["business_date"] for item in deliveries}
    last_delivered = deliveries[0]["business_date"] if deliveries else None
    return {**schedule, "deliveries": deliveries,
        "progress": {"last_delivered_date": last_delivered,
            "next_due_date": resume_date(schedule, delivered_dates),
            "delivered_count": len(deliveries),
            "blocked_date": schedule.get("blocked_date"),
            "blocked_reason": schedule.get("blocked_reason"),
            "last_error": schedule.get("last_error")}}


def resume_date(schedule, delivered_dates):
    """First not-yet-delivered due date, so progress always matches stored snapshots."""
    due = datetime.fromisoformat(schedule["next_run"]).astimezone(SHANGHAI)
    for _ in range(MAX_CATCH_UP_DAYS):
        if due.date().isoformat() not in delivered_dates:
            return due.date().isoformat()
        due += timedelta(days=1)
    return due.date().isoformat()


def listing():
    return [delivery_state(schedule) for schedule in list_rows("schedules")]


def create(body):
    if body.source_report_id is not None:
        report = get_row("reports", body.source_report_id)
        if report["status"] != "approved":
            raise HTTPException(409, "仅确认纪要可报送")
    schedule = DeliverySchedule(name=body.name, source_report_id=body.source_report_id, daily_time=body.daily_time,
        enabled=body.enabled, next_run=next_occurrence(body.daily_time, clock()))
    return insert_row("schedules", body.name, schedule.payload())


def toggle(ident, enabled):
    schedule = get_row("schedules", ident)
    changes = {"enabled": enabled, "last_error": None}
    if enabled and not schedule.get("blocked_date"):
        # A normally paused plan skips paused dates; resume at the next occurrence.
        # A blocked date is kept so re-enabling retries the day instead of skipping it.
        changes["next_run"] = next_occurrence(schedule["daily_time"], clock())
    return update_row("schedules", ident, changes)


def download(ident, business_date):
    schedule = get_row("schedules", ident)
    snapshot = find_snapshot(ident, business_date)
    if not snapshot:
        if schedule.get("blocked_date") == business_date:
            raise HTTPException(404, "该日报送被阻断：" + (schedule.get("blocked_reason") or "没有可用纪要"))
        raise HTTPException(404, "尚无该日快照，请先发起补报或等待报送时刻")
    rows = snapshot["rows"]
    venues = sorted({row["venue"] for row in rows})
    dates = sorted({row["date"] for row in rows})
    lookup = {(row["date"], row["venue"]): row for row in rows}
    output = io.StringIO()
    output.write("﻿")
    writer = csv.writer(output)
    writer.writerow(["预演日期"] + [venue + " / " + metric for venue in venues
        for metric in ("预测人数", "预测上座率(%)", "引导员建议")])
    for day in dates:
        values = []
        for venue in venues:
            row = lookup.get((day, venue))
            values.extend([row["prediction"], row["occupancy_rate"], row["ushers"]] if row else ["", "", ""])
        writer.writerow([day] + values)
    return output.getvalue()
