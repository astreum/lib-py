import threading
from typing import Optional


class MeterExceededError(Exception):
    """Raised when a Meter's limit is exceeded during charge."""


class Meter:
    def __init__(self, limit: Optional[int] = None):
        """Initialize a meter.

        Args:
            limit: Optional limit for total meter usage.

        Returns: None
        """
        self.limit: Optional[int] = limit
        self.eval: int = 0
        self.storage: int = 0
        self._lock = threading.Lock()

    @property
    def total(self) -> int:
        return self.eval + self.storage

    def remaining(self) -> int:
        """Remaining budget before the limit is hit (large int if unbounded)."""
        if self.limit is None:
            return 1 << 62
        return max(0, self.limit - self.total)

    def charge(self, n: int, kind: str = "eval") -> bool:
        """Charge the meter for resource usage.

        Args:
            n: The amount to charge.
            kind: The kind of charge ('eval' or 'storage').

        Returns:
            True if charge succeeded.

        Raises:
            MeterExceededError: If the limit would be exceeded.
        """
        if self.limit is None:
            return True
        with self._lock:
            if n < 0:
                n = 0
            if (self.total + n) > self.limit:
                raise MeterExceededError(
                    f"meter limit {self.limit} exceeded "
                    f"(used={self.total}, attempted +{n})"
                )
            if kind == "eval":
                self.eval += n
            elif kind == "storage":
                self.storage += n
            else:
                raise ValueError(f"unknown meter kind: {kind!r}")
            return True

    def charge_bytes(self, n: int, is_storage: bool = False) -> bool:
        """Charge for byte-based resource usage.

        Args:
            n: The number of bytes to charge.
            is_storage: If True, charge to storage; otherwise eval.

        Returns:
            True if charge succeeded.
        """
        return self.charge(n, kind="storage" if is_storage else "eval")
