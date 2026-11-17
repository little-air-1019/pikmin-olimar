"""重生排程與有界監控迴圈。時間都是估計，種類須重讀。"""
from __future__ import annotations

import signal
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Clock:
    now = staticmethod(time.time)
    sleep = staticmethod(time.sleep)


def next_candidate_time(expected_end: float | None, respawn: int = 300) -> float | None:
    """已觀察戰鬥完成時間 + 重生秒數；僅估計。"""
    return None if expected_end is None else expected_end + respawn


class Stopper:
    """timeout / SIGINT / stop file。"""

    def __init__(self, clock, timeout: float, stop_file: Path):
        self.clock, self.deadline, self.stop_file = clock, clock.now() + timeout, Path(stop_file)
        self.interrupted = False

    def install(self):
        """SIGINT 只設旗標：正在進行的派遣步驟會做完（不在 GO 中途被打斷），下一輪才停。回傳舊 handler 供還原。"""
        return signal.signal(signal.SIGINT, lambda *_: setattr(self, "interrupted", True))

    def reason(self) -> str | None:
        if self.interrupted:
            return "SIGINT"
        if self.stop_file.exists():
            return f"stop file {self.stop_file}"
        if self.clock.now() >= self.deadline:
            return "timeout"
        return None

    def sleep(self, secs: float) -> None:
        end = self.clock.now() + secs
        while self.clock.now() < end and not self.reason():
            self.clock.sleep(min(1.0, max(0.0, end - self.clock.now())))


def poll_interval(now: float, times, cfg) -> float:
    """任一預估重生時間（結束+respawn）附近（−near_before…+near_after 秒）→ 快速輪詢，否則慢速。預估只是提示，不能盲點。"""
    ts = [times] if isinstance(times, (int, float)) else (times or [])
    near = any(t - cfg.near_before <= now <= t + cfg.near_after for t in ts)
    return cfg.fast_interval if near else cfg.slow_interval
