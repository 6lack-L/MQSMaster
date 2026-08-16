from orchestrator.backfill.concurrent_backfill import concurrent_backfill
import pytest

from src.orchestrator.backfill.backfill_cli import DATE_FMT, build_parser

# integration + workflow_backfill apply to every test in this module. smoke is
# applied per-test so the real-execution test below can stay out of the PR gate.
pytestmark = [
    pytest.mark.integration,
    pytest.mark.workflow_backfill,
]


@pytest.mark.smoke
def test_backfill_cli_specific_parsing():
    parser = build_parser()
    args = parser.parse_args(
        [
            "specific",
            "--start",
            "010124",
            "--end",
            "010224",
            "--tickers",
            "AAPL",
            "MSFT",
            "--interval",
            "1",
            "--dry-run"
        ]
    )

    assert args.command == "specific"
    assert args.start.strftime(DATE_FMT) == "010124"
    assert args.end.strftime(DATE_FMT) == "010224"
    assert args.tickers == ["AAPL", "MSFT"]
    assert args.interval == 1
    assert args.dry_run is True


@pytest.mark.smoke
def test_concurrent_backfill_parsing():
    parser = build_parser()
    args = parser.parse_args(
        [
            "concurrent",
            "--start",
            "010124",
            "--end",
            "010224",
            "--tickers",
            "AAPL",
            "MSFT",
            "--interval",
            "5",
            "--threads",
            "4"
        ]
    )

    assert args.command == "concurrent"
    assert args.start.strftime(DATE_FMT) == "010124"
    assert args.end.strftime(DATE_FMT) == "010224"
    assert args.tickers == ["AAPL", "MSFT"]
    assert args.interval == 5
    assert args.threads == 4


@pytest.mark.db
@pytest.mark.api
def test_concurrent_backfill_execution():
    parser = build_parser()
    args = parser.parse_args(
        [
            "concurrent",
            "--start",
            "010124",
            "--end",
            "010224",
            "--tickers",
            "AAPL",
            "MSFT",
            "--interval",
            "5",
            "--threads",
            "4"
        ]
    )

    concurrent_backfill(
        tickers=args.tickers,
        start_date=args.start,
        end_date=args.end,
        interval=args.interval,
        threads=args.threads,
    )


@pytest.mark.smoke
def test_backfill_cli_inject_csv_parsing():
    parser = build_parser()
    args = parser.parse_args(
        ["inject-csv", "--csv-dir", "data/cache", "--threads", "4"]
    )

    assert args.command == "inject-csv"
    assert args.csv_dir == "data/cache"
    assert args.threads == 4
