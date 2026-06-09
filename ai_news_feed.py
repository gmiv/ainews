#!/usr/bin/env python3
"""Thin launcher for the ``ainews`` package.

Kept so the repo's run-a-script convention still works:

    python ai_news_feed.py        # run directly
    python -m ainews              # or as a module

All logic lives in the modular ``ainews/`` package next to this file.
"""
import os
import sys

# Make sure the package next to this script is importable regardless of CWD.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ainews.app import main

if __name__ == "__main__":
    main()
