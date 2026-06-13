from src.oms.order_structs import ParentOrder
from src.oms.order_structs import ChildOrder


class Scheduler:
    def __init__(self):
        self.scheduled_orders = []

    def schedule_order(self, ParentOrder: ParentOrder):
        self.scheduled_orders.append(ParentOrder)
        # In a real implementation, this would interface with a timing mechanism
        # to execute the order at the scheduled time. For now, we just store it.