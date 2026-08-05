"""Download Binance archives and run the locked two-hour research backtest."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from quantdesk.two_hour_research import (
    download_earnings_surprises,
    download_history,
    download_sec_events,
    download_sec_text_features,
    download_underlying_history,
    train_and_backtest,
)


def parser() -> argparse.ArgumentParser:
    project = Path(__file__).resolve().parents[1]
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument(
        "command",
        choices=(
            "download",
            "sec",
            "underlying",
            "earnings",
            "sec-text",
            "train",
            "run",
        ),
    )
    value.add_argument(
        "--metadata", type=Path, default=project / "config" / "tradfi_symbols.json"
    )
    value.add_argument(
        "--cache", type=Path, default=project / "data" / "two_hour_research.sqlite3"
    )
    value.add_argument(
        "--report", type=Path, default=project / "reports" / "two_hour_backtest.json"
    )
    value.add_argument(
        "--model-dir", type=Path, default=project / "reports" / "two_hour_models"
    )
    value.add_argument("--lookback-days", type=int, default=120)
    value.add_argument("--workers", type=int, default=8)
    value.add_argument("--event-lookback-days", type=int, default=730)
    value.add_argument("--underlying-lookback-days", type=int, default=700)
    value.add_argument("--seed", type=int, default=20260805)
    value.add_argument("--sample-minutes", type=int, choices=(5, 15), default=5)
    value.add_argument("--maximum-fit-rows", type=int, default=800_000)
    return value


def main() -> None:
    arguments = parser().parse_args()
    output: dict
    if arguments.command in {"download", "run"}:
        output = download_history(
            metadata_path=arguments.metadata,
            cache_path=arguments.cache,
            lookback_days=arguments.lookback_days,
            workers=arguments.workers,
        )
        print(json.dumps({"download": output}, ensure_ascii=False, indent=2))
    if arguments.command in {"sec", "run"}:
        output = download_sec_events(
            metadata_path=arguments.metadata,
            cache_path=arguments.cache,
            workers=min(arguments.workers, 4),
            lookback_days=arguments.event_lookback_days,
        )
        print(json.dumps({"sec_events": output}, ensure_ascii=False, indent=2))
    if arguments.command in {"underlying", "run"}:
        output = download_underlying_history(
            metadata_path=arguments.metadata,
            cache_path=arguments.cache,
            workers=min(arguments.workers, 4),
            lookback_days=arguments.underlying_lookback_days,
        )
        print(json.dumps({"underlying": output}, ensure_ascii=False, indent=2))
    if arguments.command in {"earnings", "run"}:
        output = download_earnings_surprises(
            cache_path=arguments.cache,
            workers=min(arguments.workers, 4),
        )
        print(json.dumps({"earnings_surprises": output}, ensure_ascii=False, indent=2))
    if arguments.command in {"sec-text", "run"}:
        output = download_sec_text_features(
            cache_path=arguments.cache,
            workers=min(arguments.workers, 8),
        )
        print(json.dumps({"sec_text": output}, ensure_ascii=False, indent=2))
    if arguments.command in {"train", "run"}:
        output = train_and_backtest(
            cache_path=arguments.cache,
            report_path=arguments.report,
            model_dir=arguments.model_dir,
            seed=arguments.seed,
            sample_minutes=arguments.sample_minutes,
            maximum_fit_rows=arguments.maximum_fit_rows,
        )
        summary = {
            "data": output["data"],
            "official_test_rows": output["official_test_rows"],
            "official_test_symbols": output["official_test_symbols"],
            "event_threshold": output["event_threshold"],
            "terminal_test": output["terminal_test"],
            "event_test": output["event_test"],
            "macro_symbol_terminal_accuracy": output["macro_symbol_terminal_accuracy"],
            "qualification": output["qualification"],
            "candidate_model_dir": output["candidate_model_dir"],
            "champion_promoted": output["champion_promoted"],
            "report": str(arguments.report),
        }
        print(json.dumps({"backtest": summary}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
