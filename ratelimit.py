"""进程内滑动窗口限流器。

无外部依赖、无持久化，专为单机 PC 服务器设计。按 (身份, 类别) 分桶，
每个桶维护一个固定窗口内的时间戳队列。身份优先用租户 token_id，
匿名/未认证回退到远端 IP。operator/developer 可在调用方豁免。

窗口计数用 deque 存请求时间戳，检查时先弹出窗口外的旧时间戳，
天然实现惰性清理，不需要后台线程。
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Rule:
    """某一类别的限额：window_seconds 秒内最多 max_requests 次。"""

    max_requests: int
    window_seconds: int


# 分级默认限额。可按需调整；ai/upload/task 比 default 严格。
DEFAULT_RULES: dict[str, Rule] = {
    "default": Rule(max_requests=120, window_seconds=60),
    "ai": Rule(max_requests=10, window_seconds=60),
    "upload": Rule(max_requests=30, window_seconds=60),
    "task": Rule(max_requests=10, window_seconds=60),
}


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    retry_after: int  # 建议客户端等待的秒数（仅在 allowed=False 时有意义）


class RateLimiter:
    """线程安全的滑动窗口限流器。"""

    def __init__(self, rules: dict[str, Rule] | None = None, *, clock=None) -> None:
        self._rules = dict(rules or DEFAULT_RULES)
        self._buckets: dict[tuple[str, str], deque[float]] = {}
        self._lock = threading.Lock()
        # 注入时钟便于测试；默认用单调时钟，不受系统时间回拨影响。
        if clock is not None:
            self._clock = clock
        else:
            import time

            self._clock = time.monotonic

    def _rule_for(self, category: str) -> Rule:
        return self._rules.get(category) or self._rules["default"]

    def check(self, category: str, identity: str) -> Decision:
        """记录一次请求并判断是否放行。放行时同时把本次时间戳计入窗口。"""

        rule = self._rule_for(category)
        now = self._clock()
        cutoff = now - rule.window_seconds
        key = (str(identity or "anon"), category)
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = deque()
                self._buckets[key] = bucket
            # 惰性清理：弹出窗口外的旧时间戳。
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= rule.max_requests:
                # 最早一次请求滑出窗口前，需要等待的秒数。
                retry_after = max(1, int(bucket[0] + rule.window_seconds - now) + 1)
                return Decision(allowed=False, retry_after=retry_after)
            bucket.append(now)
            # 空桶顺带回收，避免长期不活跃身份累积内存。
            if not bucket:
                self._buckets.pop(key, None)
            return Decision(allowed=True, retry_after=0)

    def reset(self) -> None:
        """清空所有计数（主要用于测试）。"""
        with self._lock:
            self._buckets.clear()


# 端点前缀 → 类别的归类规则。按顺序匹配 request.endpoint。
def category_for_endpoint(endpoint: str | None) -> str:
    name = endpoint or ""
    if name in ("api_chat",):
        return "ai"
    if name in ("upload_pdf", "upload_pdf_chunk"):
        return "upload"
    if name in (
        "run_rss",
        "run_pdf",
        "run_rss_discovery",
        "run_rss_publish",
    ):
        return "task"
    return "default"
