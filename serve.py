"""Production runner for the Flask app and its single task coordinator."""

from __future__ import annotations

import logging
import os
import signal
import threading
from pathlib import Path

from auth import insecure_dev_mode_requested, is_loopback_bind_host
from job_coordinator import TaskCoordinator


logger = logging.getLogger(__name__)


def _waitress_proxy_settings(environ=None):
    """Return an explicit, least-privilege Waitress reverse-proxy policy."""

    env = os.environ if environ is None else environ
    trusted_proxy = (env.get("RSSAI_TRUSTED_PROXY") or "").strip()
    if not trusted_proxy:
        return {}
    if trusted_proxy == "*":
        raise RuntimeError("RSSAI_TRUSTED_PROXY 禁止使用通配符 *")
    return {
        "trusted_proxy": trusted_proxy,
        "trusted_proxy_count": 1,
        "trusted_proxy_headers": {
            "x-forwarded-for",
            "x-forwarded-host",
            "x-forwarded-proto",
        },
        "clear_untrusted_proxy_headers": True,
    }


def _backfill_empty_tenant_feeds(registry) -> int:
    """给订阅源为空的 active 租户回填默认 OPML。

    新租户此前开局是空 OPML，导致 RSS 管线无源可抓、收不到推送。这里在启动时
    自愈：只对解析后 0 个源的租户写入默认源（幂等，已有源的租户不动），既修复
    存量租户，也兜底任何遗留的空源状态。默认源读不到时跳过，绝不阻断启动。
    """

    import tasks
    from tenancy.config_io import atomic_write_text
    from tenancy.models import TenantStatus
    from tenancy.paths import TenantPaths
    from tenancy.registry import EMPTY_OPML, read_default_opml

    default_opml = read_default_opml()
    if default_opml == EMPTY_OPML:
        logger.warning("未找到默认 feedly.opml，跳过空订阅源租户回填")
        return 0

    filled = 0
    for tenant in registry.list_tenants():
        if tenant.status is not TenantStatus.ACTIVE:
            continue
        try:
            opml_path = TenantPaths(registry.server_paths.data_root, tenant.id).opml
            if opml_path.exists() and tasks.parse_opml(str(opml_path)):
                continue
            atomic_write_text(opml_path, default_opml, tenant_id=tenant.id)
            filled += 1
            logger.info("已为空订阅源租户 %s 回填默认 RSS 源", tenant.id)
        except Exception:
            logger.exception("回填租户 %s 的默认订阅源失败", tenant.id)
    if filled:
        logger.info("共为 %s 个租户回填默认 RSS 源", filled)
    return filled


class ServerInstanceLock:
    """OS-held lock preventing two scheduler/server processes per data root."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._file = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Unbuffered I/O is required because msvcrt.locking uses the CRT file
        # position; buffered reads can leave that position at EOF.
        lock_file = self.path.open("a+b", buffering=0)
        try:
            lock_file.seek(0)
            if lock_file.read(1) == b"":
                lock_file.seek(0)
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(
                    lock_file.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
        except OSError as exc:
            lock_file.close()
            raise RuntimeError(
                f"已有服务器实例使用数据目录: {self.path.parent.parent}"
            ) from exc
        lock_file.seek(1)
        lock_file.truncate()
        lock_file.write(str(os.getpid()).encode("ascii"))
        lock_file.flush()
        self._file = lock_file

    def release(self) -> None:
        lock_file = self._file
        if lock_file is None:
            return
        try:
            lock_file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()
            self._file = None

    def __enter__(self) -> "ServerInstanceLock":
        self.acquire()
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.release()


def run(flask_app=None, server_factory=None) -> None:
    """Start the coordinator exactly once, then run Waitress."""

    if flask_app is None:
        from app import app as flask_app

    host = (os.environ.get("RSSAI_SERVER_HOST") or "127.0.0.1").strip()
    port = int(os.environ.get("RSSAI_SERVER_PORT") or 5201)
    waitress_threads = max(2, int(os.environ.get("RSSAI_WAITRESS_THREADS") or 8))
    if insecure_dev_mode_requested() and not is_loopback_bind_host(host):
        raise RuntimeError(
            "RSSAI_INSECURE_DEV_MODE=1 只能与 loopback 监听地址一起使用"
        )

    if server_factory is None:
        from waitress import create_server as server_factory

    registry = flask_app.config.get("TENANT_REGISTRY")
    if registry is None:
        from app import TENANT_REGISTRY

        registry = TENANT_REGISTRY
    instance_lock = ServerInstanceLock(
        registry.server_paths.control_dir / "server.lock"
    )
    with instance_lock:
        coordinator = TaskCoordinator.from_env(registry)
        server = server_factory(
            flask_app,
            host=host,
            port=port,
            threads=waitress_threads,
            **_waitress_proxy_settings(),
        )
        flask_app.config["BIND_HOST"] = host
        flask_app.config["TASK_COORDINATOR"] = coordinator

        previous_handlers = {}

        def request_stop(signum, _frame):
            logger.info("收到退出信号 %s，停止接收 HTTP 请求", signum)
            server.close()

        if threading.current_thread() is threading.main_thread():
            shutdown_signals = [signal.SIGINT, signal.SIGTERM]
            if hasattr(signal, "SIGBREAK"):
                shutdown_signals.append(signal.SIGBREAK)
            for signum in shutdown_signals:
                try:
                    previous_handlers[signum] = signal.signal(
                        signum,
                        request_stop,
                    )
                except (OSError, ValueError):
                    pass

        try:
            _backfill_empty_tenant_feeds(registry)
            coordinator.start()
            logger.info(
                "启动服务器: %s:%s (waitress_threads=%s, job_workers=%s, "
                "job_queue=%s)",
                host,
                port,
                waitress_threads,
                coordinator.max_workers,
                coordinator.max_pending,
            )
            server.run()
        except KeyboardInterrupt:
            logger.info("收到键盘中断，准备关闭")
        finally:
            server.close()
            logger.info("等待后台任务到达安全结束点")
            coordinator.shutdown(wait=True)
            flask_app.config.pop("TASK_COORDINATOR", None)
            for signum, handler in previous_handlers.items():
                try:
                    signal.signal(signum, handler)
                except (OSError, ValueError):
                    pass
            logger.info("服务器已关闭")


def main() -> int:
    from app import app

    try:
        run(app)
    except RuntimeError as exc:
        if str(exc).startswith("已有服务器实例使用数据目录:"):
            logger.error("%s", exc)
            return 2
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
