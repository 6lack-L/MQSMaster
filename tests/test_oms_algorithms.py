"""Unit coverage for the OMS execution-algorithm layer.

Everything here is pure in-memory (no DB, no API): TWAP/VWAP/MARKET schedule
generation, algorithm selection from the OMS config block, the manage_order
pump (fills, retry-once-then-cancel, expiry), cancellation, and the monitor's
slippage math. The engine wiring (who calls manage_order and with what
execute_child) is a separate follow-up and is NOT covered here.
"""

from datetime import datetime, timedelta

import pytest

from src.oms.monitor import OrderMonitor
from src.oms.order_manager import OrderManager
from src.oms.order_structs import AlgoType, ChildOrder, OrderStatus, ParentOrder, Side, tz
from src.oms.sizing import build_algorithm
from src.oms.sizing.twap import TWAPAlgorithm
from src.oms.sizing.vwap import VWAPAlgorithm

pytestmark = [
    pytest.mark.smoke,
    pytest.mark.workflow_backtest,
]

T0 = tz.localize(datetime(2026, 1, 5, 10, 0, 0))


def _parent(quantity=1000.0, duration=30, side=Side.BUY, algo=AlgoType.TWAP):
    parent = ParentOrder()
    parent.portfolio_id = "1"
    parent.ticker = "AAPL"
    parent.signal_type = side
    parent.total_quantity = quantity
    parent.algo_type = algo
    parent.duration_minutes = duration
    parent.arrival_price = 100.0
    parent.created_at = T0
    parent.updated_at = T0
    return parent


class TestTWAP:
    def test_equal_slices_and_spacing(self):
        children = TWAPAlgorithm(num_slices=10).generate_schedule(_parent(1000.0, 30))
        assert len(children) == 10
        assert all(c.target_quantity == 100.0 for c in children)
        assert children[0].scheduled_time == T0
        assert children[1].scheduled_time - children[0].scheduled_time == timedelta(minutes=3)
        assert sum(c.target_quantity for c in children) == 1000.0

    def test_remainder_goes_to_last_slice(self):
        children = TWAPAlgorithm(num_slices=10).generate_schedule(_parent(105.0, 30))
        assert [c.target_quantity for c in children[:-1]] == [10.0] * 9
        assert children[-1].target_quantity == 15.0

    def test_small_order_caps_slice_count(self):
        # 3 shares cannot fill 10 slices; never emit zero-share children.
        children = TWAPAlgorithm(num_slices=10).generate_schedule(_parent(3.0, 30))
        assert len(children) == 3
        assert all(c.target_quantity == 1.0 for c in children)

    def test_children_inherit_parent_identity(self):
        parent = _parent(100.0, 30, side=Side.SELL)
        children = TWAPAlgorithm(num_slices=4).generate_schedule(parent)
        assert all(c.parent_order_id == parent.order_id for c in children)
        assert all(c.signal_type == Side.SELL for c in children)
        assert [c.slice_index for c in children] == [0, 1, 2, 3]


class TestVWAP:
    def test_profile_weighted_slices(self):
        children = VWAPAlgorithm(bucket_minutes=15).generate_schedule(
            _parent(1000.0, 45), volume_profile=[0.5, 0.3, 0.2]
        )
        assert [c.target_quantity for c in children] == [500.0, 300.0, 200.0]
        assert [c.scheduled_time for c in children] == [
            T0,
            T0 + timedelta(minutes=15),
            T0 + timedelta(minutes=30),
        ]

    def test_remainder_goes_to_highest_weight_bucket(self):
        # 100 * [0.335, 0.335, 0.33] floors to 33+33+33 = 99; the extra
        # share lands in the first (max-weight) bucket.
        children = VWAPAlgorithm(bucket_minutes=15).generate_schedule(
            _parent(100.0, 45), volume_profile=[0.335, 0.335, 0.33]
        )
        assert sum(c.target_quantity for c in children) == 100.0
        assert children[0].target_quantity == 34.0

    def test_missing_profile_falls_back_to_uniform(self):
        children = VWAPAlgorithm(bucket_minutes=15).generate_schedule(
            _parent(900.0, 45), volume_profile=None
        )
        assert [c.target_quantity for c in children] == [300.0, 300.0, 300.0]

    def test_invalid_profile_falls_back_to_uniform(self):
        for bad in ([0.5, 0.5], [0.0, 0.0, 0.0], [-1.0, 1.0, 1.0], "nope"):
            children = VWAPAlgorithm(bucket_minutes=15).generate_schedule(
                _parent(900.0, 45), volume_profile=bad
            )
            assert [c.target_quantity for c in children] == [300.0, 300.0, 300.0]

    def test_zero_quantity_buckets_are_skipped(self):
        children = VWAPAlgorithm(bucket_minutes=15).generate_schedule(
            _parent(2.0, 45), volume_profile=[0.9, 0.05, 0.05]
        )
        assert all(c.target_quantity > 0 for c in children)
        assert sum(c.target_quantity for c in children) == 2.0


class TestAlgorithmFactory:
    def test_market_single_immediate_child(self):
        children = build_algorithm(AlgoType.MARKET, {}).generate_schedule(
            _parent(500.0, 30, algo=AlgoType.MARKET)
        )
        assert len(children) == 1
        assert children[0].target_quantity == 500.0
        assert children[0].scheduled_time == T0

    def test_unimplemented_algo_raises(self):
        with pytest.raises(NotImplementedError):
            build_algorithm(AlgoType.LIMIT, {})


def _fill_all(child: ChildOrder):
    """execute_child stub that fully fills every slice at $101."""
    return {"filled_quantity": child.target_quantity, "fill_price": 101.0}


def _manager(config=None):
    return OrderManager(portfolio_id="1", config=config or {})


def _submit(manager, quantity=100.0, **kwargs):
    return manager.process_order(
        portfolio_id="1",
        ticker="AAPL",
        side="BUY",
        confidence=0.8,
        arrival_price=100.0,
        total_quantity=quantity,
        timestamp=T0,
        **kwargs,
    )


class TestOrderManagerLifecycle:
    def test_process_order_slices_and_goes_working(self):
        manager = _manager(
            {"default_algo": "TWAP", "twap_num_slices": 4, "duration_minutes": 20}
        )
        parent = _submit(manager, 100.0)
        assert parent.status == OrderStatus.WORKING
        assert parent.algo_type == AlgoType.TWAP
        children = manager.children_by_parent[parent.order_id]
        assert len(children) == 4
        assert manager.scheduler.pending_count() == 4

    def test_pump_fills_due_slices_only(self):
        manager = _manager(
            {"default_algo": "TWAP", "twap_num_slices": 4, "duration_minutes": 20}
        )
        parent = _submit(manager, 100.0)
        # At t0 only the first slice (t+0) is due.
        attempted = manager.manage_order(now=T0, execute_child=_fill_all)
        assert len(attempted) == 1
        assert parent.status == OrderStatus.PARTIALLY_FILLED
        assert parent.filled_quantity == 25.0
        # Past the full window, everything fills.
        manager.manage_order(now=T0 + timedelta(minutes=20), execute_child=_fill_all)
        assert parent.status == OrderStatus.FILLED
        assert parent.filled_quantity == 100.0
        assert parent.avg_fill_price == 101.0

    def test_default_algo_from_config_and_market_default(self):
        assert _submit(_manager({"default_algo": "VWAP"})).algo_type == AlgoType.VWAP
        assert _submit(_manager({})).algo_type == AlgoType.MARKET

    def test_min_order_notional_downgrades_to_market(self):
        manager = _manager({"default_algo": "TWAP", "min_order_notional": 100000.0})
        parent = _submit(manager, 100.0)  # notional 10k < 100k threshold
        assert parent.algo_type == AlgoType.MARKET
        assert len(manager.children_by_parent[parent.order_id]) == 1

    def test_unknown_default_algo_respects_fallback_flag(self):
        parent = _submit(_manager({"default_algo": "SNIPER"}))
        assert parent.algo_type == AlgoType.MARKET
        with pytest.raises(ValueError):
            _submit(
                _manager({"default_algo": "SNIPER", "fallback_to_market": False})
            )

    def test_failed_child_retries_once_then_cancels_and_parent_expires(self):
        manager = _manager({"default_algo": "MARKET"})
        parent = _submit(manager, 100.0)

        def always_fail(child):
            return None

        # Attempt 1 fails -> re-queued; parent still working.
        assert len(manager.manage_order(now=T0, execute_child=always_fail)) == 1
        assert parent.status == OrderStatus.WORKING
        # Attempt 2 (the single retry) fails -> child cancelled, parent
        # expired with zero fills still on the books.
        later = T0 + timedelta(seconds=60)
        assert len(manager.manage_order(now=later, execute_child=always_fail)) == 1
        (child,) = manager.children_by_parent[parent.order_id]
        assert child.status == OrderStatus.CANCELLED
        assert parent.status == OrderStatus.EXPIRED
        assert parent.filled_quantity == 0.0

    def test_partial_child_fill_expires_parent_with_partial_on_books(self):
        manager = _manager({"default_algo": "MARKET"})
        parent = _submit(manager, 100.0)

        def half_fill(child):
            return {"filled_quantity": child.target_quantity / 2, "fill_price": 101.0}

        manager.manage_order(now=T0, execute_child=half_fill)
        assert parent.status == OrderStatus.EXPIRED
        assert parent.filled_quantity == 50.0

    def test_cancel_order_drops_queued_children(self):
        manager = _manager(
            {"default_algo": "TWAP", "twap_num_slices": 4, "duration_minutes": 20}
        )
        parent = _submit(manager, 100.0)
        manager.cancel_order(parent.order_id)
        assert parent.status == OrderStatus.CANCELLED
        assert all(
            c.status == OrderStatus.CANCELLED
            for c in manager.children_by_parent[parent.order_id]
        )
        # The pump releases nothing for a cancelled parent.
        assert (
            manager.manage_order(
                now=T0 + timedelta(hours=1), execute_child=_fill_all
            )
            == []
        )

    def test_pump_without_executor_preserves_queue(self):
        manager = _manager({"default_algo": "MARKET"})
        _submit(manager, 100.0)
        assert manager.manage_order(now=T0) == []
        assert manager.scheduler.pending_count() == 1


class TestOrderMonitor:
    def test_slippage_sign_is_side_aware(self):
        buy = _parent(side=Side.BUY)
        buy.filled_quantity = 100.0
        buy.avg_fill_price = 101.0  # paid up on a BUY -> adverse -> positive
        assert OrderMonitor.slippage_bps(buy) == pytest.approx(100.0)

        sell = _parent(side=Side.SELL)
        sell.filled_quantity = 100.0
        sell.avg_fill_price = 101.0  # sold above arrival -> improvement -> negative
        assert OrderMonitor.slippage_bps(sell) == pytest.approx(-100.0)

    def test_snapshot_shape(self):
        manager = _manager({"default_algo": "MARKET"})
        _submit(manager, 100.0)
        (row,) = OrderMonitor.snapshot(manager)
        assert row["ticker"] == "AAPL"
        assert row["algo"] == "MARKET"
        assert row["status"] == OrderStatus.WORKING.value
