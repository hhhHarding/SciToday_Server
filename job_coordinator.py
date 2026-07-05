"""Bounded, tenant-aware background job coordination.

The coordinator is the only component allowed to submit background work.  It
keeps request-local tenant context out of worker threads, applies bounded
backpressure, and persists scheduler state in control.db.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Mapping

import tasks
from tenancy.context import OWNER_TENANT_ID, tenant_context
from tenancy.models import TenantStatus
from tenancy.registry import TenantRegistry


logger = logging.getLogger(__name__)

# shared_ingest: owner-only，用 owner Key 抓取并集订阅源并消化进共享缓存。
# rss_deliver: 每个租户从共享缓存投递命中自己订阅源的文章（评分用租户 Key）。
# 旧 rss/rss_discovery/rss_publish 保留仅为兼容历史 job_state 行与现有手动端点，
# 均已重定向到共享缓存语义（见 tasks.py）。
JOB_TYPES = (
    "shared_ingest",
    "rss_deliver",
    "rss",
    "pdf",
    "rss_discovery",
    "rss_publish",
    "interest_profile",
)

# 旧的按租户抓取/发布任务类型：新逻辑下一律停用其调度。
LEGACY_RSS_JOB_TYPES = ("rss", "rss_discovery", "rss_publish")
@dataclass(frozen=True, slots=True)
class JobRequest:
    tenant_id: str
    job_type: str
    request_id: str
    trigger_source: str
    scheduled: bool = False
    interval_seconds: int = 0
    force: bool = False

    @property
    def key(self) -> tuple[str, str]:
        return self.tenant_id, self.job_type


@dataclass(frozen=True, slots=True)
class SubmitResult:
    accepted: bool
    reason: str
    request_id: str


class TaskCoordinator:
    """Run tenant jobs through one fixed-size executor and one scanner thread."""

    def __init__(
        self,
        registry: TenantRegistry,
        *,
        max_workers: int = 2,
        max_pending: int = 8,
        scan_interval: int = 30,
        lease_seconds: int = 3600,
        handlers: Mapping[str, Callable[..., object]] | None = None,
        clock: Callable[[], float] = time.time,
    ):
        if int(max_workers) < 1:
            raise ValueError("max_workers 必须至少为 1")
        if int(max_pending) < 0:
            raise ValueError("max_pending 不能小于 0")
        if int(scan_interval) < 1:
            raise ValueError("scan_interval 必须至少为 1 秒")

        self.registry = registry
        self.max_workers = int(max_workers)
        self.max_pending = int(max_pending)
        self.scan_interval = int(scan_interval)
        self.lease_seconds = max(1, int(lease_seconds))
        self._clock = clock
        self._handlers = dict(handlers or {})
        self._executor = ThreadPoolExecutor(
            max_workers=self.max_workers,
            thread_name_prefix="rssai-job",
        )
        # The semaphore bounds running + queued work. ThreadPoolExecutor's
        # internal queue is otherwise unbounded.
        self._capacity = threading.BoundedSemaphore(
            self.max_workers + self.max_pending
        )
        self._lock = threading.RLock()
        self._active_keys: set[tuple[str, str]] = set()
        self._futures: dict[Future[object], JobRequest] = {}
        self._progress: dict[tuple[str, str], dict[str, object]] = {}
        self._interest_force_again: set[str] = set()
        self._accepting = False
        self._started = False
        self._shutdown = False
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._scanner_thread: threading.Thread | None = None

    @classmethod
    def from_env(cls, registry: TenantRegistry) -> "TaskCoordinator":
        return cls(
            registry,
            max_workers=int(os.environ.get("RSSAI_JOB_WORKERS") or 2),
            max_pending=int(os.environ.get("RSSAI_JOB_QUEUE_SIZE") or 8),
            scan_interval=int(os.environ.get("RSSAI_JOB_SCAN_SECONDS") or 30),
            lease_seconds=int(os.environ.get("RSSAI_JOB_LEASE_SECONDS") or 3600),
        )

    def start(self, *, start_scanner: bool = True) -> bool:
        """Start once. Returns False when already started."""

        with self._lock:
            if self._shutdown:
                raise RuntimeError("任务协调器已关闭，不能重新启动")
            if self._started:
                return False
            self.registry.initialize()
            recovered = self.registry.recover_incomplete_jobs(
                now=int(self._clock())
            )
            self._accepting = True
            self._started = True
            tasks.configure_interest_job_submitter(self._submit_interest_job)
            if start_scanner:
                self._scanner_thread = threading.Thread(
                    target=self._scanner_loop,
                    name="rssai-job-scanner",
                    daemon=False,
                )
                self._scanner_thread.start()
        if recovered:
            logger.warning("已恢复 %s 个未完成任务", recovered)
        return True

    def submit(
        self,
        tenant_id: str,
        job_type: str,
        *,
        trigger_source: str,
        request_id: str | None = None,
        scheduled: bool = False,
        interval_seconds: int = 0,
        force: bool = False,
    ) -> SubmitResult:
        normalized_type = str(job_type or "").strip()
        if normalized_type not in JOB_TYPES:
            raise ValueError(f"未知任务类型: {normalized_type}")
        request = JobRequest(
            tenant_id=str(tenant_id),
            job_type=normalized_type,
            request_id=request_id or uuid.uuid4().hex,
            trigger_source=str(trigger_source or "unknown")[:80],
            scheduled=bool(scheduled),
            interval_seconds=max(0, int(interval_seconds)),
            force=bool(force),
        )

        with self._lock:
            if not self._started or not self._accepting:
                return SubmitResult(False, "stopped", request.request_id)
            if request.key in self._active_keys:
                return SubmitResult(False, "duplicate", request.request_id)
            if not self._capacity.acquire(blocking=False):
                if request.scheduled:
                    self.registry.defer_job(
                        request.tenant_id,
                        request.job_type,
                        reason="executor queue full",
                        next_run_at=int(self._clock()) + self.scan_interval,
                        request_id=request.request_id,
                    )
                return SubmitResult(False, "queue_full", request.request_id)
            self._active_keys.add(request.key)
            self._progress[request.key] = self._new_progress(
                active=True,
                message="等待执行...",
                request_id=request.request_id,
                trigger_source=request.trigger_source,
            )

        try:
            self.registry.record_job_queued(
                request.tenant_id,
                request.job_type,
                request_id=request.request_id,
                trigger_source=request.trigger_source,
                now=int(self._clock()),
            )
            future = self._executor.submit(self._run_job, request)
        except Exception:
            with self._lock:
                self._active_keys.discard(request.key)
                self._progress[request.key] = self._new_progress(
                    active=False,
                    message="提交失败",
                    request_id=request.request_id,
                    trigger_source=request.trigger_source,
                )
            self._capacity.release()
            raise

        with self._lock:
            self._futures[future] = request
        future.add_done_callback(self._future_done)
        return SubmitResult(True, "accepted", request.request_id)

    def snapshot(self, tenant_id: str) -> dict[str, dict[str, object]]:
        """Return only one tenant's in-memory state."""

        with self._lock:
            running = {
                job_type: (str(tenant_id), job_type) in self._active_keys
                for job_type in JOB_TYPES
            }
            progress = {
                job_type: dict(
                    self._progress.get(
                        (str(tenant_id), job_type),
                        self._new_progress(active=False),
                    )
                )
                for job_type in JOB_TYPES
            }
        return {"running": running, "progress": progress}

    def wake_scheduler(self) -> None:
        self._wake_event.set()

    def scan_once(self, *, now: int | None = None) -> None:
        """Synchronize schedules and enqueue due jobs. Public for verification."""

        checked_at = int(self._clock() if now is None else now)
        for tenant in self.registry.list_tenants():
            if tenant.status is not TenantStatus.ACTIVE:
                continue
            try:
                with tenant_context(tenant.id):
                    schedule = (tasks.load_config().get("schedule") or {})
                enabled = bool(schedule.get("enabled", True))
                publish_minutes = self._positive_minutes(
                    schedule.get("rss_interval_minutes", 30),
                    30,
                )
                discovery_minutes = self._positive_minutes(
                    schedule.get(
                        "rss_discovery_interval_minutes",
                        publish_minutes,
                    ),
                    publish_minutes,
                )
                pdf_minutes = self._positive_minutes(
                    schedule.get("pdf_interval_minutes", 5),
                    5,
                )
                # 每个租户从共享缓存投递（用 rss_interval_minutes）并各自扫 PDF。
                intervals = {
                    "rss_deliver": publish_minutes * 60,
                    "pdf": pdf_minutes * 60,
                }
                # 共享消化只在 owner 排程一次（用 discovery 间隔），避免每个租户各抓一遍。
                if tenant.id == OWNER_TENANT_ID:
                    intervals["shared_ingest"] = discovery_minutes * 60
                for job_type, interval in intervals.items():
                    self.registry.ensure_job_schedule(
                        tenant.id,
                        job_type,
                        interval_seconds=interval,
                        enabled=enabled,
                        now=checked_at,
                    )
                # 停用历史按租户抓取/发布任务，防止旧 job_state 行继续触发。
                for legacy in LEGACY_RSS_JOB_TYPES:
                    self.registry.ensure_job_schedule(
                        tenant.id,
                        legacy,
                        interval_seconds=publish_minutes * 60,
                        enabled=False,
                        now=checked_at,
                    )
            except Exception:
                logger.exception("同步租户 %s 的调度配置失败", tenant.id)

        for row in self.registry.list_due_jobs(now=checked_at):
            self.submit(
                row["tenant_id"],
                row["job_type"],
                trigger_source="scheduler",
                scheduled=True,
                interval_seconds=int(row["interval_seconds"]),
            )

    def shutdown(self, *, wait: bool = True) -> None:
        """Stop submissions, stop scanning, and optionally await safe completion."""

        with self._lock:
            if self._shutdown:
                return
            self._accepting = False
            self._shutdown = True
            self._stop_event.set()
            self._wake_event.set()
            scanner = self._scanner_thread

        tasks.configure_interest_job_submitter(None)
        if scanner and scanner is not threading.current_thread():
            scanner.join(timeout=max(2, self.scan_interval + 1))

        # Work already running is allowed to reach its normal safe return point.
        # Pending executor work is retained when wait=True to preserve accepted
        # requests; process shutdown therefore has no silent task loss.
        self._executor.shutdown(wait=wait, cancel_futures=not wait)

    def _run_job(self, request: JobRequest) -> object:
        started_at = int(self._clock())
        self.registry.record_job_running(
            request.tenant_id,
            request.job_type,
            request_id=request.request_id,
            lease_seconds=self.lease_seconds,
            now=started_at,
        )
        self._set_progress(
            request,
            active=True,
            current=0,
            total=0,
            message="执行中...",
        )
        try:
            with tenant_context(request.tenant_id):
                tasks.record_event(
                    "task",
                    f"{request.job_type} 任务开始",
                    details={
                        "request_id": request.request_id,
                        "trigger_source": request.trigger_source,
                    },
                )
                result = self._invoke_handler(request)
                tasks.record_event(
                    "task",
                    f"{request.job_type} 任务完成",
                    details={
                        "request_id": request.request_id,
                        "trigger_source": request.trigger_source,
                        "result": result,
                    },
                )
            finished_at = int(self._clock())
            next_run_at = (
                finished_at + request.interval_seconds
                if request.scheduled and request.interval_seconds
                else None
            )
            self.registry.record_job_finished(
                request.tenant_id,
                request.job_type,
                request_id=request.request_id,
                success=True,
                next_run_at=next_run_at,
                now=finished_at,
            )
            self._set_progress(request, active=False, message="已完成")
            return result
        except Exception as exc:
            logger.exception(
                "租户 %s 的 %s 任务失败",
                request.tenant_id,
                request.job_type,
            )
            try:
                with tenant_context(request.tenant_id):
                    tasks.record_event(
                        "task",
                        f"{request.job_type} 任务失败",
                        level="error",
                        details={
                            "request_id": request.request_id,
                            "trigger_source": request.trigger_source,
                            "error": str(exc),
                        },
                    )
            except Exception:
                logger.exception("写入任务失败事件时发生异常")
            finished_at = int(self._clock())
            next_run_at = (
                finished_at + request.interval_seconds
                if request.scheduled and request.interval_seconds
                else None
            )
            self.registry.record_job_finished(
                request.tenant_id,
                request.job_type,
                request_id=request.request_id,
                success=False,
                error=str(exc),
                next_run_at=next_run_at,
                now=finished_at,
            )
            self._set_progress(
                request,
                active=False,
                message=f"失败: {str(exc)[:200]}",
            )
            raise

    def _invoke_handler(self, request: JobRequest) -> object:
        handler = self._handlers.get(request.job_type)
        if handler is None:
            handler = {
                "shared_ingest": tasks.run_shared_rss_ingest,
                "rss_deliver": tasks.deliver_shared_to_tenant,
                # owner 的 rss = 消化并集 + 立即给自己投递；其它租户 rss = 仅投递。
                "rss": (
                    tasks.run_shared_rss_ingest
                    if request.tenant_id == OWNER_TENANT_ID
                    else tasks.deliver_shared_to_tenant
                ),
                "pdf": tasks.run_pdf_watch,
                # 兼容旧手动端点：discovery≈消化，publish≈投递。
                "rss_discovery": (
                    tasks.run_shared_rss_ingest
                    if request.tenant_id == OWNER_TENANT_ID
                    else tasks.deliver_shared_to_tenant
                ),
                "rss_publish": tasks.deliver_shared_to_tenant,
                "interest_profile": tasks.refresh_interest_profile,
            }[request.job_type]
        if request.job_type == "interest_profile":
            return handler(force=request.force)
        return handler(progress_callback=self._progress_callback(request))

    def _progress_callback(self, request: JobRequest) -> Callable[..., None]:
        def callback(current: int, total: int, message: str = "") -> None:
            self._set_progress(
                request,
                active=True,
                current=current,
                total=total,
                message=message,
            )

        return callback

    def _set_progress(
        self,
        request: JobRequest,
        *,
        active: bool,
        current: int | None = None,
        total: int | None = None,
        message: str | None = None,
    ) -> None:
        with self._lock:
            state = dict(
                self._progress.get(
                    request.key,
                    self._new_progress(
                        active=active,
                        request_id=request.request_id,
                        trigger_source=request.trigger_source,
                    ),
                )
            )
            state["active"] = active
            if current is not None:
                state["current"] = current
            if total is not None:
                state["total"] = total
            if message is not None:
                state["message"] = message
            self._progress[request.key] = state

    def _future_done(self, future: Future[object]) -> None:
        resubmit_interest = False
        with self._lock:
            request = self._futures.pop(future, None)
            if request is not None:
                self._active_keys.discard(request.key)
                if (
                    request.job_type == "interest_profile"
                    and request.tenant_id in self._interest_force_again
                    and self._accepting
                ):
                    self._interest_force_again.discard(request.tenant_id)
                    resubmit_interest = True
        self._capacity.release()
        if future.cancelled() and request is not None:
            self.registry.defer_job(
                request.tenant_id,
                request.job_type,
                reason="cancelled during coordinator shutdown",
                next_run_at=int(self._clock()) + self.scan_interval,
                request_id=request.request_id,
            )
        # Retrieve the exception so failed jobs do not produce an unobserved
        # Future warning. The worker already logged and persisted it.
        if not future.cancelled():
            try:
                future.exception()
            except Exception:
                pass
        if resubmit_interest and request is not None:
            self.submit(
                request.tenant_id,
                "interest_profile",
                trigger_source="interest_profile_coalesced",
                force=True,
            )

    def _scanner_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.scan_once()
            except Exception:
                logger.exception("任务调度扫描失败")
            self._wake_event.wait(self.scan_interval)
            self._wake_event.clear()

    def _submit_interest_job(self, tenant_id: str, force: bool) -> bool:
        result = self.submit(
            tenant_id,
            "interest_profile",
            trigger_source="interest_profile",
            force=force,
        )
        if result.reason == "duplicate" and force:
            with self._lock:
                self._interest_force_again.add(str(tenant_id))
        return result.accepted or result.reason == "duplicate"

    @staticmethod
    def _positive_minutes(value: object, default: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = int(default)
        return max(1, parsed)

    @staticmethod
    def _new_progress(
        *,
        active: bool,
        message: str = "",
        request_id: str = "",
        trigger_source: str = "",
    ) -> dict[str, object]:
        return {
            "active": active,
            "current": 0,
            "total": 0,
            "message": message,
            "request_id": request_id,
            "trigger_source": trigger_source,
        }
