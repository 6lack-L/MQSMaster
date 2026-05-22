from src.oms.order_structs import OrderStatus, ParentOrder, ChildOrder, AlgoType, Side

import logging
from datetime import datetime
import pytz
import pandas as pd

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class OrderManager:
    def __init__(self):
        self.orders = []  # List to store all orders
        self.child_orders = []  # List to store child orders
        self.parent_order_ids = set()  # Set to track unique parent order IDs

    def process_order(self, parent_order: ParentOrder):
        # Check for duplicate parent order ID
        if parent_order.order_id in self.parent_order_ids:
            raise ValueError(f"Duplicate parent order ID: {parent_order.order_id}")
        self.parent_order_ids.add(parent_order.order_id)
        self.orders.append(parent_order)




    def manage_order(orders: list[ParentOrder]):
        # Placeholder for order management logic (e.g., updating order status, handling fills)
        pass