"""Deterministic uuid5 identity helpers shared by gridbot and recorder.

Both processes must agree on `user_id`, `account_id`, and `strategy_id` so they
can write to the same DB rows without collisions or FK divergence.
"""

from uuid import UUID, uuid5

UUID_NAMESPACE = UUID("12345678-1234-5678-1234-567812345678")


def account_id_for(name: str) -> str:
    return str(uuid5(UUID_NAMESPACE, f"account:{name}"))


def user_id_for(name: str) -> str:
    return str(uuid5(UUID_NAMESPACE, f"user:{name}"))


def strategy_id_for(strat_id: str) -> str:
    return str(uuid5(UUID_NAMESPACE, f"strategy:{strat_id}"))
