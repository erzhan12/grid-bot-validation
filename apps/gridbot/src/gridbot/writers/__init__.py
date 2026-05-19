"""Gridbot writers — sync, threading-based DB writers.

Distinct from ``apps/event_saver/.../writers`` which are asyncio-based and
live in the recorder process. Gridbot's main loop is synchronous (uses
``time.sleep``), so writers here mirror ``gridcore.persistence.GridStateStore``'s
sync API + background ``threading.Thread`` pattern.
"""

from gridbot.writers.grid_state_writer import GridStateWriter

__all__ = ["GridStateWriter"]
