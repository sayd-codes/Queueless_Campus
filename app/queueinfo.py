"""Queue helpers shared by the emails, the user screens and the API."""
from __future__ import annotations

import math

from . import config, db


def estimate_wait_min(counter: str, ahead: int) -> int:
    """Rough minutes until service for someone with `ahead` people in front of them."""
    cfg = config.COUNTERS.get(counter, {})
    service = cfg.get("service_min", config.DEFAULT_SERVICE_MIN)
    staff = max(int(cfg.get("staff", config.DEFAULT_STAFF)), 1)
    return int(math.ceil(max(ahead, 0) * service / staff))


def snapshot(token_id: int, path: str | None = None) -> dict:
    """People ahead of a waiting token and the estimated wait. Both None if not waiting."""
    token = db.get_token(token_id, path)
    ahead = db.queue_position(token_id, path)
    if token is None or ahead is None:
        return {"ahead": None, "est_wait_min": None}
    return {"ahead": ahead, "est_wait_min": estimate_wait_min(token["counter"], ahead)}


def counter_status(counter: str, path: str | None = None) -> dict:
    """What a newcomer would face right now at a counter."""
    waiting = db.waiting_count(counter, path)
    return {"counter": counter, "waiting": waiting, "est_wait_min": estimate_wait_min(counter, waiting)}
