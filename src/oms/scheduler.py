"""Poll-driven child-order scheduler.

Holds every scheduled ``ChildOrder`` in a min-heap keyed by
``scheduled_time`` and releases the ones that are due when asked. The queue
itself owns **no clock and no thread** — each pipeline drives it with its
own notion of time:

  * backtest: the ``BacktestRunner`` event loop pumps
    ``OrderManager.manage_order(now=sim_time, ...)`` at every bar;
  * live: ``RunEngine`` runs one dedicated ``oms-pump`` thread (default 5s
    tick — finer than any portfolio poll interval) that pumps with
    wall-clock NY time. That thread lives in the engine layer on purpose:
    the queue works identically under both clocks and stays trivially
    testable, while the engine owns lifecycle (start, shutdown drain).

The queue is lock-protected because in live the pump thread pops while
portfolio threads enqueue.

Time semantics: ``scheduled_time`` and the ``now`` passed to ``pop_due``
must both be timezone-aware in the repo convention (America/New_York, see
``order_structs.tz``). Mixing naive and aware datetimes raises ``TypeError``
by Python's own comparison rules — that is a caller bug, not something the
scheduler papers over.
"""

import heapq
import itertools
import threading
from datetime import datetime
from typing import List

from src.oms.order_structs import ChildOrder, OrderStatus, ParentOrder


class Scheduler:
    def __init__(self):
        # Registry of every parent handed to the scheduler, in arrival order.
        # Kept for observability/back-compat (pre-algorithm code appended
        # parents here); the executable queue is the child-order heap below.
        self.scheduled_orders: List[ParentOrder] = []

        # Min-heap of (scheduled_time, sequence, child). The monotonically
        # increasing sequence number breaks timestamp ties so heapq never
        # falls through to comparing ChildOrder objects (which don't order),
        # and preserves FIFO among same-time slices.
        self._heap: list = []
        self._sequence = itertools.count()

        # Parents cancelled while their children are still queued. Children
        # are removed lazily: they stay in the heap and are dropped (marked
        # CANCELLED) when they surface in pop_due — O(1) cancel, no heap
        # rebuild.
        self._cancelled_parents: set[str] = set()

        # One lock covers heap + cancel set. Today each OMS instance is only
        # touched from its own portfolio thread, but cancel_parent is the kind
        # of call a future ops/console thread would make, so the queue is
        # thread-safe now rather than retrofitted later.
        self._lock = threading.Lock()

    def schedule_order(self, parent_order: ParentOrder):
        """Register a parent order. Its children are enqueued separately via
        ``enqueue_children`` (the OrderManager generates them through the
        execution algorithm)."""
        with self._lock:
            self.scheduled_orders.append(parent_order)

    def enqueue_children(self, children: List[ChildOrder]):
        """Add child orders to the time queue. Children without a
        ``scheduled_time`` are rejected loudly — a silently unscheduled slice
        would strand its parent in WORKING forever."""
        with self._lock:
            for child in children:
                if child.scheduled_time is None:
                    raise ValueError(
                        f"Child order {child.child_id} (parent "
                        f"{child.parent_order_id}) has no scheduled_time"
                    )
                heapq.heappush(
                    self._heap, (child.scheduled_time, next(self._sequence), child)
                )

    def pop_due(self, now: datetime) -> List[ChildOrder]:
        """Remove and return every child with ``scheduled_time <= now``.

        Children of cancelled parents are marked CANCELLED and *not*
        returned. Callers get each child exactly once — a child that fails to
        execute must be re-enqueued explicitly (the OrderManager owns that
        retry policy), which is what makes one pop_due per pump tick safe
        from same-tick retry loops.
        """
        due: List[ChildOrder] = []
        with self._lock:
            while self._heap and self._heap[0][0] <= now:
                _, _, child = heapq.heappop(self._heap)
                if child.parent_order_id in self._cancelled_parents:
                    child.status = OrderStatus.CANCELLED
                    child.updated_at = now
                    continue
                due.append(child)
        return due

    def cancel_parent(self, parent_order_id: str):
        """Drop all still-queued children of a parent (lazily, see above).
        Called by ``OrderManager.cancel_order``."""
        with self._lock:
            self._cancelled_parents.add(parent_order_id)

    def pending_count(self) -> int:
        """Number of children still queued (including ones that will be
        dropped as cancelled when popped) — a cheap health metric for logs."""
        with self._lock:
            return len(self._heap)

    def has_pending(self, parent_order_id: str) -> bool:
        """True if any queued child belongs to ``parent_order_id`` and its
        parent is not cancelled. Used by the OrderManager to decide whether a
        parent's schedule is exhausted (O(n); the queue is small — slices per
        working order per portfolio)."""
        with self._lock:
            if parent_order_id in self._cancelled_parents:
                return False
            return any(
                child.parent_order_id == parent_order_id
                for _, _, child in self._heap
            )
