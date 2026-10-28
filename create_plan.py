#!/usr/bin/env python3
"""
create_plan.py — backwards-compatible entry point, identical to frigilunch.cli.

    py -m frigilunch.cli --week 36 --year 2026
    py create_plan.py   --week 36 --year 2026   # same thing

All flags (--dry-run, --warmup, --render-only, --suffix, --reset) are
available in both commands.
"""
from frigilunch.cli import main

if __name__ == "__main__":
    main()
