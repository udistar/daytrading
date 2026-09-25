"""토큰버킷, TR 제한, 우선순위 큐."""

from __future__ import annotations

import heapq
from datetime import datetime

from daytrading.models import Intent
from daytrading.settings import Settings


class RateGate:
    """초당 토큰버킷과 모의투자 TR당 호출 간격을 함께 적용한다."""

    def __init__(self, settings: Settings):
        self.rate = float(settings.token_bucket_per_sec)
        self.tokens = self.rate
        self.updated: datetime | None = None
        self.tr_interval = 1.0 / float(settings.paper_tr_per_sec)
        self.tr_last: dict[str, datetime] = {}

    def ready(self, tr_id: str, now: datetime) -> bool:
        self._refill(now)
        last = self.tr_last.get(tr_id)
        if last is not None and (now - last).total_seconds() + 1e-9 < self.tr_interval:
            return False
        return self.tokens >= 1

    def take(self, tr_id: str, now: datetime) -> bool:
        if not self.ready(tr_id, now):
            return False
        self.tokens -= 1
        self.tr_last[tr_id] = now
        return True

    def _refill(self, now: datetime) -> None:
        if self.updated is None:
            self.updated = now
            self.tokens = self.rate
            return
        elapsed = (now - self.updated).total_seconds()
        if elapsed < 0:
            return
        self.updated = now
        self.tokens = min(self.rate, self.tokens + elapsed * self.rate)


class OrderQueue:
    def __init__(self) -> None:
        self._heap: list[tuple[int, int, Intent]] = []
        self._seq = 0

    def __len__(self) -> int:
        return len(self._heap)

    def push(self, intent: Intent) -> None:
        heapq.heappush(self._heap, (intent.priority, self._seq, intent))
        self._seq += 1

    def peek(self) -> Intent | None:
        if not self._heap:
            return None
        return self._heap[0][2]

    def pop(self) -> Intent:
        return heapq.heappop(self._heap)[2]
