"""北京时间边界、日期与定时任务计算。"""

from __future__ import annotations

import datetime as dt
from typing import Iterable

from .foundation import BEIJING, Commit, WorkflowError, parse_clock_time, parse_weekday

def current_run_time() -> dt.datetime:
    return dt.datetime.now(tz=BEIJING)

def as_beijing(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=BEIJING)
    return value.astimezone(BEIJING)

def scheduled_daily_date(
    schedule_time: str, now: dt.datetime | None = None
) -> dt.date:
    """计算最近一次应执行的每日统计日期，适配任务计划程序延迟唤醒。"""
    now = as_beijing(now or current_run_time())
    scheduled = dt.datetime.combine(
        now.date(), parse_clock_time(schedule_time, "daily.time"), tzinfo=BEIJING
    )
    if now < scheduled:
        return now.date() - dt.timedelta(days=1)
    return now.date()

def scheduled_weekly_end(
    weekday: str, schedule_time: str, now: dt.datetime | None = None
) -> dt.datetime:
    """计算最近一次应执行的周测结束时间，而不是使用实际唤醒时间。"""
    now = as_beijing(now or current_run_time())
    target_weekday = parse_weekday(weekday)
    days_since_target = (now.weekday() - target_weekday) % 7
    candidate_date = now.date() - dt.timedelta(days=days_since_target)
    candidate = dt.datetime.combine(
        candidate_date, parse_clock_time(schedule_time, "weekly.time"), tzinfo=BEIJING
    )
    if now < candidate:
        candidate -= dt.timedelta(days=7)
    return candidate

def parse_date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise WorkflowError(f"日期格式应为 YYYY-MM-DD：{value}") from exc

def parse_datetime(value: str | None) -> dt.datetime:
    if not value:
        return current_run_time()
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise WorkflowError(
            "时间格式应为 ISO 8601，例如 2026-07-26T08:00:00+08:00"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BEIJING)
    return parsed.astimezone(BEIJING)

def all_commits_in_local_day(commits: Iterable[Commit], target_date: dt.date) -> list[Commit]:
    return sorted(
        [commit for commit in commits if commit.committed_at.date() == target_date],
        key=lambda commit: commit.committed_at,
    )
