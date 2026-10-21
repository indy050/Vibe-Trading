"""Command line entry: ``python -m src.us30_strategy <bars.csv>`` (run from ``agent/``)."""

from .backtest import main

raise SystemExit(main())
