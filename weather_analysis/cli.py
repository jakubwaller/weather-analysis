"""Command line interface.

  weather-analysis collect            one collection run
  weather-analysis collect --loop     collect forever on the configured interval
  weather-analysis backfill --days 30 fetch past data (Open-Meteo + HA recorder)
  weather-analysis demo               seed synthetic data to try the dashboard
  weather-analysis dashboard          start the Streamlit dashboard
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .config import ConfigError, load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="weather-analysis",
        description="Collect outside weather and Home Assistant data, analyse with graphs.",
    )
    parser.add_argument("-c", "--config", help="path to config.yaml (default: ./config.yaml)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_collect = sub.add_parser("collect", help="collect current readings")
    p_collect.add_argument("--loop", action="store_true",
                           help="keep collecting on the configured interval")

    p_backfill = sub.add_parser("backfill", help="fetch past data")
    p_backfill.add_argument("--days", type=int, default=30,
                            help="how many days back (Open-Meteo max 92; HA limited "
                                 "by recorder retention). Default 30")

    p_demo = sub.add_parser("demo", help="seed synthetic demo data")
    p_demo.add_argument("--days", type=int, default=30, help="days of demo data (default 30)")

    sub.add_parser("dashboard", help="start the Streamlit dashboard")

    args = parser.parse_args(argv)

    try:
        if args.command == "collect":
            from .collect import collect_loop, collect_once
            config = load_config(args.config)
            if args.loop:
                collect_loop(config)
            else:
                collect_once(config)
        elif args.command == "backfill":
            from .collect import backfill
            config = load_config(args.config)
            backfill(config, args.days)
        elif args.command == "demo":
            from .demo import seed_demo_data
            db_path = "data/weather.db"
            try:
                db_path = load_config(args.config).db_path
            except ConfigError:
                pass  # no config yet is fine for the demo
            seed_demo_data(db_path, args.days)
        elif args.command == "dashboard":
            db_path = "data/weather.db"
            try:
                db_path = str(load_config(args.config).db_path)
            except ConfigError:
                pass
            app = Path(__file__).resolve().parent.parent / "dashboard" / "app.py"
            os.environ["WEATHER_DB"] = db_path
            os.execvp(sys.executable,
                      [sys.executable, "-m", "streamlit", "run", str(app)])
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
