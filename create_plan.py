#!/usr/bin/env python3
"""
create_plan.py — backwards-compatible entry point, identical to frigilunch.cli.

    py -m frigilunch.cli --week 36 --year 2026
    py create_plan.py   --week 36 --year 2026   # same thing

Note: --dry-run, --warmup, --render-only, --suffix from the old create_plan.py
are not yet wired into frigilunch.cli and are temporarily unavailable.
"""
from frigilunch.cli import main

if __name__ == "__main__":
    main()
