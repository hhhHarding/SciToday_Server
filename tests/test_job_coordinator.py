import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import tasks
from job_coordinator import TaskCoordinator
from server_config import ServerPaths
from tenancy.context import OWNER_TENANT_ID, get_current_tenant_id
from tenancy.registry import CONTROL_SCHEMA_VERSION, TenantRegistry


class TaskCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.paths = ServerPaths(Path(self.temp_dir.name) / "server")
        self.registry = TenantRegistry(self.paths)
        self.registry.create_tenant("Owner", tenant_id="owner")
        self.registry.create_tenant("Alpha", tenant_id="t_alpha")
        self.registry.create_tenant("Beta", tenant_id="t_beta")
        self.paths_patch = patch.object(tasks, "SERVER_PATHS", self.paths)
        self.event_patch = patch.object(tasks, "record_event", return_value=None)
        self.paths_patch.start()
        self.event_patch.start()
        tasks._reset_config_cache_for_tests()
        tasks._reset_migration_cache_for_tests()
        self.coordinators = []

    def tearDown(self):
        for coordinator in reversed(self.coordinators):
            coordinator.shutdown(wait=True)
        tasks.configure_interest_job_submitter(None)
        tasks._reset_config_cache_for_tests()
        tasks._reset_migration_cache_for_tests()
        self.event_patch.stop()
        self.paths_patch.stop()
        self.temp_dir.cleanup()

    def coordinator(self, **kwargs):
        coordinator = TaskCoordinator(self.registry, **kwargs)
        self.coordinators.append(coordinator)
        coordinator.start(start_scanner=False)
        return coordinator

    @staticmethod
    def wait_until(predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return False

    def test_schema_v3_is_idempotent_and_has_job_state(self):
        self.assertEqual(self.registry.initialize(), CONTROL_SCHEMA_VERSION)
        self.assertEqual(self.registry.initialize(), CONTROL_SCHEMA_VERSION)
        con = self.registry._connect()
        try:
            tables = {
                row[0]
                for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            version = con.execute("PRAGMA user_version").fetchone()[0]
        finally:
            con.close()
        self.assertIn("job_state", tables)
        self.assertEqual(version, 3)

    def test_same_tenant_job_is_deduplicated_but_tenants_run_concurrently(self):
        release = threading.Event()
        started = []
        started_lock = threading.Lock()

        def blocking_handler(progress_callback=None):
            with started_lock:
                started.append(get_current_tenant_id())
            release.wait(5)
            return "ok"

        coordinator = self.coordinator(
            max_workers=2,
            max_pending=0,
            scan_interval=30,
            handlers={"pdf": blocking_handler},
        )
        first = coordinator.submit("t_alpha", "pdf", trigger_source="test")
        duplicate = coordinator.submit("t_alpha", "pdf", trigger_source="test")
        second_tenant = coordinator.submit("t_beta", "pdf", trigger_source="test")

        self.assertTrue(first.accepted)
        self.assertEqual(duplicate.reason, "duplicate")
        self.assertTrue(second_tenant.accepted)
        self.assertTrue(self.wait_until(lambda: len(started) == 2))
        self.assertEqual(set(started), {"t_alpha", "t_beta"})
        self.assertEqual(get_current_tenant_id(), OWNER_TENANT_ID)
        release.set()
        self.assertTrue(
            self.wait_until(
                lambda: not any(
                    coordinator.snapshot(tenant)["running"]["pdf"]
                    for tenant in ("t_alpha", "t_beta")
                )
            )
        )

    def test_full_queue_defers_scheduled_work_instead_of_spawning(self):
        release = threading.Event()

        def blocking_handler(progress_callback=None):
            release.wait(5)

        coordinator = self.coordinator(
            max_workers=1,
            max_pending=0,
            scan_interval=17,
            handlers={"pdf": blocking_handler, "rss_publish": blocking_handler},
        )
        first = coordinator.submit("t_alpha", "pdf", trigger_source="test")
        rejected = coordinator.submit(
            "t_beta",
            "rss_publish",
            trigger_source="scheduler",
            scheduled=True,
            interval_seconds=60,
        )

        self.assertTrue(first.accepted)
        self.assertEqual(rejected.reason, "queue_full")
        state = {
            row["job_type"]: row
            for row in self.registry.get_job_states("t_beta")
        }["rss_publish"]
        self.assertEqual(state["status"], "deferred")
        self.assertIn("queue full", state["last_error"])
        release.set()

    def test_progress_and_persisted_state_are_tenant_scoped(self):
        release = threading.Event()

        def handler(progress_callback=None):
            progress_callback(2, 5, f"{get_current_tenant_id()}-only")
            release.wait(5)

        coordinator = self.coordinator(
            max_workers=1,
            max_pending=1,
            scan_interval=30,
            handlers={"pdf": handler},
        )
        result = coordinator.submit("t_alpha", "pdf", trigger_source="manual_api")
        self.assertTrue(result.accepted)
        self.assertTrue(
            self.wait_until(
                lambda: coordinator.snapshot("t_alpha")["progress"]["pdf"][
                    "message"
                ]
                == "t_alpha-only"
            )
        )
        alpha = coordinator.snapshot("t_alpha")
        beta = coordinator.snapshot("t_beta")
        self.assertTrue(alpha["running"]["pdf"])
        self.assertFalse(beta["running"]["pdf"])
        self.assertEqual(beta["progress"]["pdf"]["message"], "")
        row = {
            item["job_type"]: item
            for item in self.registry.get_job_states("t_alpha")
        }["pdf"]
        self.assertEqual(row["request_id"], result.request_id)
        self.assertEqual(row["trigger_source"], "manual_api")
        self.assertEqual(row["status"], "running")
        release.set()

    def test_scanner_creates_schedules_and_enqueues_due_jobs(self):
        observed = []
        observed_lock = threading.Lock()

        def handler(progress_callback=None):
            with observed_lock:
                observed.append(
                    (get_current_tenant_id(), threading.current_thread().name)
                )

        coordinator = self.coordinator(
            max_workers=3,
            max_pending=8,
            scan_interval=30,
            handlers={
                "shared_ingest": handler,
                "rss_deliver": handler,
                "pdf": handler,
            },
        )
        coordinator.scan_once(now=100)
        owner_states = {
            row["job_type"]: row for row in self.registry.get_job_states("owner")
        }
        # 只有 owner 排程共享消化；投递与 PDF 每个租户都有；旧任务类型停用。
        enabled_owner = {jt for jt, row in owner_states.items() if row["enabled"]}
        self.assertEqual(enabled_owner, {"shared_ingest", "rss_deliver", "pdf"})
        alpha_states = {
            row["job_type"]: row for row in self.registry.get_job_states("t_alpha")
        }
        enabled_alpha = {jt for jt, row in alpha_states.items() if row["enabled"]}
        self.assertEqual(enabled_alpha, {"rss_deliver", "pdf"})
        for legacy in ("rss", "rss_discovery", "rss_publish"):
            self.assertFalse(alpha_states[legacy]["enabled"])

        coordinator.scan_once(now=2000)
        # owner:3 + t_alpha:2 + t_beta:2 = 7 个到期任务。
        self.assertTrue(self.wait_until(lambda: len(observed) == 7))
        self.assertEqual(
            {tenant for tenant, _thread in observed},
            {"owner", "t_alpha", "t_beta"},
        )
        self.assertTrue(
            all(thread_name.startswith("rssai-job") for _, thread_name in observed)
        )

    def test_start_recovers_queued_and_running_records(self):
        self.registry.record_job_queued(
            "t_alpha",
            "pdf",
            request_id="old-queued",
            trigger_source="test",
            now=10,
        )
        self.registry.record_job_running(
            "t_beta",
            "rss_publish",
            request_id="old-running",
            lease_seconds=60,
            now=10,
        )
        coordinator = self.coordinator(
            max_workers=1,
            max_pending=0,
            scan_interval=30,
        )
        del coordinator
        alpha = self.registry.get_job_states("t_alpha")[0]
        beta = self.registry.get_job_states("t_beta")[0]
        self.assertEqual(alpha["status"], "deferred")
        self.assertEqual(beta["status"], "deferred")
        self.assertIn("process restart", alpha["last_error"])

    def test_shutdown_stops_accepting_and_waits_for_running_job(self):
        started = threading.Event()
        release = threading.Event()

        def handler(progress_callback=None):
            started.set()
            release.wait(5)

        coordinator = self.coordinator(
            max_workers=1,
            max_pending=0,
            scan_interval=30,
            handlers={"pdf": handler},
        )
        coordinator.submit("t_alpha", "pdf", trigger_source="test")
        self.assertTrue(started.wait(5))
        shutdown_thread = threading.Thread(
            target=coordinator.shutdown,
            kwargs={"wait": True},
        )
        shutdown_thread.start()
        self.assertTrue(
            self.wait_until(
                lambda: coordinator.submit(
                    "t_beta", "pdf", trigger_source="test"
                ).reason
                == "stopped"
            )
        )
        self.assertTrue(shutdown_thread.is_alive())
        release.set()
        shutdown_thread.join(5)
        self.assertFalse(shutdown_thread.is_alive())

    def test_interest_refresh_is_deduplicated_and_force_is_coalesced(self):
        first_started = threading.Event()
        release = threading.Event()
        calls = []

        def interest_handler(force=False):
            calls.append((get_current_tenant_id(), force))
            if len(calls) == 1:
                first_started.set()
                release.wait(5)

        coordinator = self.coordinator(
            max_workers=1,
            max_pending=1,
            scan_interval=30,
            handlers={"interest_profile": interest_handler},
        )
        self.assertTrue(coordinator._submit_interest_job("t_alpha", False))
        self.assertTrue(first_started.wait(5))
        self.assertTrue(coordinator._submit_interest_job("t_alpha", True))
        release.set()
        self.assertTrue(self.wait_until(lambda: len(calls) == 2))
        self.assertEqual(calls, [("t_alpha", False), ("t_alpha", True)])


if __name__ == "__main__":
    unittest.main()
