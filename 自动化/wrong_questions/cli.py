"""命令行参数解析与工作流调度。"""

from __future__ import annotations

import argparse
import json
import sys

from .foundation import ROOT, WorkflowError, load_config, load_dotenv
from .git_store import verify_tracked_ref
from .daily_workflow import daily_report
from .weekly_workflow import weekly_reports
from .review_workflow import review_reports
from .correction_workflow import correction_reports
from .checks import check_workflow
from .question_index import generate_question_index
from .audit import audit_repository, write_audit_reports, audit_markdown
from .scheduling import current_run_time, parse_date, parse_datetime, scheduled_daily_date, scheduled_weekly_end

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="每日错题自动化工作流")
    subparsers = parser.add_subparsers(dest="command", required=True)

    daily = subparsers.add_parser("daily", help="生成每日统计")
    daily.add_argument("--date", help="按指定北京时间日期运行：YYYY-MM-DD")
    daily.add_argument(
        "--scheduled",
        action="store_true",
        help="按配置中的每日时间计算最近一次应统计的日期，适合任务计划程序延迟运行",
    )
    daily.add_argument("--no-ai", action="store_true", help="不调用外部 AI，仅生成结构检查报告")
    daily.add_argument("--dry-run", action="store_true", help="生成预览到终端，不写入报告文件")
    daily.add_argument("--tracked-ref", help="覆盖配置中的生产 Git ref，例如 refs/heads/main")

    weekly = subparsers.add_parser("weekly", help="生成数学和 408 周测")
    weekly.add_argument("--at", help="指定周测结束时间，例如 2026-07-26T08:00:00+08:00")
    weekly.add_argument(
        "--scheduled",
        action="store_true",
        help="按配置中的星期和时间计算最近一次周测结束时间，适合任务计划程序延迟运行",
    )
    weekly.add_argument("--no-ai", action="store_true", help="不调用外部 AI，仅生成无题目占位报告")
    weekly.add_argument("--dry-run", action="store_true", help="生成预览到终端，不写入报告文件")
    weekly.add_argument("--tracked-ref", help="覆盖配置中的生产 Git ref，例如 refs/heads/main")

    review = subparsers.add_parser("review", help="生成复盘状态、掌握度测试和独立答案")
    review.add_argument("--date", help="复盘日期：YYYY-MM-DD，默认使用北京时间当天")
    review.add_argument("--subject", help="只生成指定科目，例如数学或 408")
    review.add_argument("--no-ai", action="store_true", help="不调用外部 AI，仅生成追踪状态和结构报告")
    review.add_argument("--dry-run", action="store_true", help="生成预览到终端，不写入报告文件")
    review.add_argument("--tracked-ref", help="覆盖配置中的生产 Git ref，例如 refs/heads/main")

    correct = subparsers.add_parser("correct", help="逐篇审校笔记并生成纠错报告")
    correct.add_argument("--date", help="报告日期：YYYY-MM-DD，默认使用北京时间当天")
    correct.add_argument("--subject", help="只检查指定科目，例如数学或 408")
    correct.add_argument("--no-ai", action="store_true", help="不调用外部 AI，仅检查笔记、题图和链接结构")
    correct.add_argument("--dry-run", action="store_true", help="生成预览到终端，不写入报告文件")
    correct.add_argument("--tracked-ref", help="覆盖配置中的生产 Git ref，例如 refs/heads/main")

    check = subparsers.add_parser("check", help="只读检查 Git 和题图/笔记解析，不写入报告")
    check.add_argument("--date", help="检查指定每日日期：YYYY-MM-DD")
    check.add_argument("--at", help="检查指定周测结束时间：ISO 8601")
    check.add_argument("--tracked-ref", help="覆盖配置中的生产 Git ref，例如 refs/heads/main")

    index = subparsers.add_parser("index", help="生成稳定 Question ID 索引")
    index.add_argument("--subject", help="只处理指定科目")
    index.add_argument("--dry-run", action="store_true", help="只预览，不写入索引")
    index.add_argument("--tracked-ref", help="覆盖配置中的生产 Git ref，例如 refs/heads/main")

    audit = subparsers.add_parser("audit", help="只读审计全仓库结构、来源和报告状态")
    audit.add_argument("--dry-run", action="store_true", help="只输出结果，不写入审计报告")
    audit.add_argument("--json", action="store_true", help="dry-run 时只输出 JSON")
    audit.add_argument("--tracked-ref", help="覆盖配置中的生产 Git ref，例如 refs/heads/main")
    return parser

def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config()
        if args.tracked_ref:
            config.setdefault("git", {})["tracked_ref"] = args.tracked_ref
        verify_tracked_ref(str(config.get("git", {}).get("tracked_ref", "refs/heads/main")))
        if args.command == "daily":
            if args.date and args.scheduled:
                raise WorkflowError("daily 不能同时使用 --date 和 --scheduled。")
            if args.scheduled:
                target_date = scheduled_daily_date(str(config["daily"]["time"]))
            else:
                target_date = parse_date(args.date) if args.date else current_run_time().date()
            path = daily_report(
                target_date,
                config,
                use_ai=not args.no_ai,
                write=not args.dry_run,
            )
            if not args.dry_run:
                print(f"已生成：{path}")
            return 0
        if args.command == "weekly":
            if args.at and args.scheduled:
                raise WorkflowError("weekly 不能同时使用 --at 和 --scheduled。")
            if args.scheduled:
                end = scheduled_weekly_end(
                    str(config["weekly"]["weekday"]), str(config["weekly"]["time"])
                )
            else:
                end = parse_datetime(args.at)
            outputs = weekly_reports(
                end,
                config,
                use_ai=not args.no_ai,
                write=not args.dry_run,
            )
            if not args.dry_run:
                for path in outputs:
                    print(f"已生成：{path}")
            return 0
        if args.command == "review":
            target_date = (
                parse_date(args.date) if args.date else current_run_time().date()
            )
            outputs = review_reports(
                target_date,
                config,
                use_ai=not args.no_ai,
                write=not args.dry_run,
                subject_filter=args.subject,
            )
            if not args.dry_run:
                for path in outputs:
                    print(f"已生成：{path}")
            return 0
        if args.command == "correct":
            target_date = (
                parse_date(args.date) if args.date else current_run_time().date()
            )
            outputs = correction_reports(
                target_date,
                config,
                use_ai=not args.no_ai,
                write=not args.dry_run,
                subject_filter=args.subject,
            )
            if not args.dry_run:
                for path in outputs:
                    print(f"已生成：{path}")
            return 0
        if args.command == "check":
            target_date = parse_date(args.date) if args.date else None
            at = parse_datetime(args.at) if args.at else None
            print(json.dumps(check_workflow(target_date, at, config), ensure_ascii=False, indent=2))
            return 0
        if args.command == "index":
            value, problems = generate_question_index(
                config, dry_run=args.dry_run, subject_filter=args.subject
            )
            print(json.dumps({"dry_run": args.dry_run, "index": value, "problems": problems}, ensure_ascii=False, indent=2))
            return 0 if not problems else 2
        if args.command == "audit":
            result = audit_repository(config)
            audit_dir = ROOT / str(config["reports"]["audit"])
            date_text = current_run_time().strftime("%Y-%m-%d")
            json_path = audit_dir / f"仓库审计-{date_text}.json"
            markdown_path = audit_dir / f"仓库审计-{date_text}.md"
            if args.dry_run:
                if args.json:
                    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
                else:
                    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
                    print(audit_markdown(result))
            else:
                write_audit_reports(result, json_path, markdown_path)
                print(f"已生成：{json_path}")
                print(f"已生成：{markdown_path}")
            return 0 if result.status == "validated" else 2
    except WorkflowError as exc:
        print(f"工作流失败：{exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # 保留清晰的自动化失败出口，便于任务计划程序记录
        print(f"工作流出现未处理错误：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 3
    return 1
