#!/usr/bin/env python
"""Thin CLI wrapper. Real logic lives in `recorder.prepare_session.main`."""

from recorder.prepare_session import main


if __name__ == "__main__":
    raise SystemExit(main())
