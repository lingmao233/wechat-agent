from __future__ import annotations

import argparse
from datetime import datetime, time, timedelta, timezone
import json
import logging
import sys
from pathlib import Path

from .agent import WeChatAgent
from .config import Settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Guarded Weixin 4.1.x AI test-group agent")
    parser.add_argument(
        "command",
        choices=["ready", "run", "init-db", "desktop", "rebuild-memory", "export-training-data"],
        nargs="?",
        default="ready",
    )
    parser.add_argument("--config", help="optional .env path", default=None)
    parser.add_argument("--window", type=int, help="messages used to rebuild group profiles")
    parser.add_argument(
        "--out",
        default=None,
        help="export directory; --today defaults to data/training/daily/<date>",
    )
    parser.add_argument(
        "--today",
        action="store_true",
        help="export only messages from the local calendar day (for nightly jobs)",
    )
    parser.add_argument(
        "--context",
        type=int,
        default=6,
        help="prior messages per exported behavior example",
    )
    parser.add_argument(
        "--no-anonymize",
        action="store_true",
        help="keep real member/group names in the local training export",
    )
    parser.add_argument("--allow-send", action="store_true", help="enable sending; requires group allowlist")
    parser.add_argument("--stop", action="store_true", help="create the configured STOP file and exit")
    parser.add_argument("--host", help="desktop console bind host")
    parser.add_argument("--port", type=int, help="desktop console bind port")
    parser.add_argument("--open-browser", action="store_true", help="open the local desktop console")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.config:
        # pydantic-settings reads the selected file without mutating the
        # process-wide environment.
        settings = Settings(_env_file=Path(args.config))
    else:
        settings = Settings()
    if args.host:
        settings.desktop_host = args.host
    if args.port:
        settings.desktop_port = args.port
    if args.allow_send:
        settings.wechat_allow_send = True
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.stop:
        settings.stop_file.parent.mkdir(parents=True, exist_ok=True)
        settings.stop_file.write_text("stop\n", encoding="utf-8")
        print(f"created {settings.stop_file}")
        return 0

    if args.command == "desktop":
        from .desktop import serve

        serve(
            settings,
            open_browser=args.open_browser,
            config_path=Path(args.config).expanduser() if args.config else Path(".env"),
        )
        return 0

    if args.command == "export-training-data":
        from .storage import Storage
        from .training_data import export_training_data

        storage = Storage(settings)
        storage.init_schema()
        since = until = None
        if args.today:
            now = datetime.now().astimezone()
            day = now.date()
            start = datetime.combine(day, time.min, tzinfo=now.tzinfo)
            since = start.astimezone(timezone.utc)
            until = (start + timedelta(days=1)).astimezone(timezone.utc)
            out = args.out or f"data/training/daily/{day.isoformat()}"
        else:
            out = args.out or "data/training"
        report = export_training_data(
            storage,
            out,
            window_messages=args.context,
            anonymize=not args.no_anonymize,
            since=since,
            until=until,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    agent = WeChatAgent(settings)
    if args.command == "init-db":
        print("database schema initialized")
        return 0
    if args.command == "rebuild-memory":
        report = agent.rebuild_memory(window_messages=args.window)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if args.command == "ready":
        print(json.dumps(agent.ready(), ensure_ascii=False, indent=2))
        return 0
    agent.run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
