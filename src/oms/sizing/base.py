from abc import ABC, abstractmethod
from typing import List, Optional

from src.oms.order_structs import ChildOrder, ParentOrder


class BaseAlgorithm(ABC):
    """All execution algorithms implement this interface."""

    @abstractmethod
    def generate_schedule(
        self,
        parent_order: ParentOrder,
        volume_profile: Optional[List[float]] = None,
    ) -> List[ChildOrder]:
        """
        Given a parent order, produce a list of child orders
        with target quantities and scheduled execution times.
        """
        ...
