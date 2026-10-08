"""命令行入口：uv run python -m app sync --backfill"""

from __future__ import annotations

import argparse
import logging

from app.pipeline import run_sync


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="机器人操作论文雷达")
    sub = parser.add_subparsers(dest="command", required=True)
    sync = sub.add_parser("sync", help="抓取并按规则入库")
    sync.add_argument("--backfill", action="store_true", help="从时间窗起点回填，可重复运行以继续游标")
    sync.add_argument("--reconcile", action="store_true", help="重读会议快照，并回看最近 180 天")
    sync.add_argument("--only", default="", help="只跑这些源，逗号分隔，例如 virtual,rss")
    sync.add_argument("--venue", default="", help="只处理这个会议，例如 CVPR")
    sync.add_argument("--year", type=int, default=0, help="只处理这一年")
    args = parser.parse_args()
    if args.command == "sync":
        if args.backfill:
            mode = "backfill"
        elif args.reconcile:
            mode = "reconcile"
        else:
            mode = "incremental"
        message = run_sync(
            mode,
            only=[item for item in args.only.split(",") if item],
            venue=args.venue,
            year=args.year or None,
        )
        print(message)


if __name__ == "__main__":
    main()
