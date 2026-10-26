from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .agent import WeChatAgent
from .config import Settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Guarded Weixin 4.1.x AI test-group agent")
    parser.add_argument("command", choices=["ready", "run", "init-db"], nargs="?", default="ready")
    parser.add_argument("--config", help="optional .env path", default=None)
    parser.add_argument("--allow-send", action="store_true", help="enable sending; requires group allowlist")
    parser.add_argument("--stop", action="store_true", help="create the configured STOP file and exit")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.config:
        # pydantic-settings reads the selected file without mutating the
        # process-wide environment.
        settings = Settings(_env_file=Path(args.config))
    else:
        settings = Settings()
    if args.allow_send:
        settings.wechat_allow_send = True
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.stop:
        settings.stop_file.parent.mkdir(parents=True, exist_ok=True)
        settings.stop_file.write_text("stop\n", encoding="utf-8")
        print(f"created {settings.stop_file}")
        return 0

    agent = WeChatAgent(settings)
    if args.command == "init-db":
        print("database schema initialized")
        return 0
    if args.command == "ready":
        print(json.dumps(agent.ready(), ensure_ascii=False, indent=2))
        return 0
    agent.run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
