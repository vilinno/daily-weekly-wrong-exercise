"""只读环境、Git 与来源解析检查。"""

from __future__ import annotations

import datetime as dt
from typing import Any

from .foundation import ROOT, load_config
from .git_store import collect_changed_paths, commits_for_daily, commits_for_week, read_commits, read_uncommitted_paths, subject_for_path
from .source_scanner import build_subject_bundle

def check_workflow(target_date: dt.date | None = None, at: dt.datetime | None = None) -> dict[str, Any]:
    config = load_config()
    commits = read_commits()
    dirty_paths = read_uncommitted_paths()
    result: dict[str, Any] = {
        "timezone": "Asia/Shanghai",
        "root": str(ROOT),
        "uncommitted_source_paths": sorted(
            path
            for path in dirty_paths
            if subject_for_path(path, config.get("subjects", {})) is not None
        ),
        "allowed_commit_examples": [
            "daily: YYYY-MM-DD",
            "weekly: YYYY-Www",
            "docs: 简短说明",
            "chore: 简短说明",
            "fix: 简短说明",
        ],
    }
    if target_date:
        daily = commits_for_daily(commits, target_date)
        changed = collect_changed_paths(daily)
        result["daily"] = {
            "date": target_date.isoformat(),
            "commits": [commit.message for commit in daily],
            "changed_paths": sorted(changed),
            "subjects": {
                subject: {
                    "sources": len(
                        build_subject_bundle(
                            subject, changed, path, dirty_paths, commits=daily
                        ).sources
                    ),
                    "problems": build_subject_bundle(
                        subject, changed, path, dirty_paths, commits=daily
                    ).problems,
                }
                for subject, path in config.get("subjects", {}).items()
            },
        }
    if at:
        start = at - dt.timedelta(days=int(config["weekly"].get("window_days", 7)))
        weekly = commits_for_week(commits, start, at)
        changed = collect_changed_paths(weekly)
        result["weekly"] = {
            "start": start.isoformat(),
            "end": at.isoformat(),
            "commits": [commit.message for commit in weekly],
            "changed_paths": sorted(changed),
        }
    return result
