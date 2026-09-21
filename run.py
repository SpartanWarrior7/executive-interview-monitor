#!/usr/bin/env python3
"""Activate the interview monitor.

    python run.py                 # check for new interviews
    python run.py --seed          # first run: baseline what already exists
    python run.py --format json   # machine-readable output
"""

from interview_monitor.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
