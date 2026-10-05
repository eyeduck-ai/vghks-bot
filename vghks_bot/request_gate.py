"""Allow parallel API requests while protecting database lifetime changes."""
from contextlib import contextmanager
from threading import Condition

from .jobs import BusyError


class RequestGate:
    def __init__(self):
        self.condition = Condition()
        self.active = 0
        self.exclusive = False

    @contextmanager
    def lease(self, *, exclusive=False):
        # The mutex only protects admission counters, never request execution.
        with self.condition:
            if self.exclusive or exclusive and self.active:
                raise BusyError("資料庫正在使用中；請等待目前操作完成後再試。")
            if exclusive:
                self.exclusive = True
            else:
                self.active += 1
        try:
            yield
        finally:
            with self.condition:
                if exclusive:
                    self.exclusive = False
                else:
                    self.active -= 1
                self.condition.notify_all()
