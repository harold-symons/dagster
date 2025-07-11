import multiprocessing
import os
import sys
import tempfile
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

import dagster as dg
import pytest
import sqlalchemy
import sqlalchemy as db
from dagster import DagsterInstance
from dagster._core.events import EngineEventData, SerializableErrorInfo, StepRetryData
from dagster._core.execution.stats import (
    StepEventStatus,
    build_run_stats_from_events,
    build_run_step_stats_from_events,
    build_run_step_stats_snapshot_from_events,
)
from dagster._core.storage.event_log import (
    ConsolidatedSqliteEventLogStorage,
    SqlEventLogStorageMetadata,
    SqlEventLogStorageTable,
    SqliteEventLogStorage,
)
from dagster._core.storage.event_log.schema import ConcurrencyLimitsTable, ConcurrencySlotsTable
from dagster._core.storage.legacy_storage import LegacyEventLogStorage
from dagster._core.storage.sql import create_engine
from dagster._core.storage.sqlalchemy_compat import db_select
from dagster._core.storage.sqlite_storage import DagsterSqliteStorage
from dagster._core.utils import make_new_run_id
from dagster._utils.test import ConcurrencyEnabledSqliteTestEventLogStorage
from sqlalchemy import __version__ as sqlalchemy_version
from sqlalchemy.engine import Connection

from dagster_tests.storage_tests.utils.event_log_storage import (
    TestEventLogStorage,
    _synthesize_events,
)


class TestInMemoryEventLogStorage(TestEventLogStorage):
    __test__ = True

    @pytest.fixture(scope="function", name="storage")
    def event_log_storage(self, instance):  # pyright: ignore[reportIncompatibleMethodOverride]
        yield instance.event_log_storage

    @pytest.fixture(name="instance", scope="function")
    def instance(self):  # pyright: ignore[reportIncompatibleMethodOverride]
        with DagsterInstance.ephemeral() as the_instance:
            yield the_instance

    def can_wipe_asset_partitions(self) -> bool:
        return False

    @pytest.mark.skipif(
        sys.version_info >= (3, 12) and sqlalchemy_version.startswith("1.4."),
        reason="flaky Sqlite issues on certain version combinations",
    )
    def test_basic_get_logs_for_run_multiple_runs_cursors(self, instance, storage):
        super().test_basic_get_logs_for_run_multiple_runs_cursors(instance, storage)


class TestSqliteEventLogStorage(TestEventLogStorage):
    __test__ = True

    # TestSqliteEventLogStorage::test_asset_wiped_event

    @pytest.fixture(name="instance", scope="function")
    def instance(self):  # pyright: ignore[reportIncompatibleMethodOverride]
        with tempfile.TemporaryDirectory(dir=os.getcwd()) as tmpdir_path:
            with dg.instance_for_test(temp_dir=tmpdir_path) as instance:
                yield instance

    @pytest.fixture(scope="function", name="storage")
    def event_log_storage(self, instance):  # pyright: ignore[reportIncompatibleMethodOverride]
        event_log_storage = instance.event_log_storage
        assert isinstance(event_log_storage, SqliteEventLogStorage)
        yield instance.event_log_storage

    def supports_multiple_event_type_queries(self):  # pyright: ignore[reportIncompatibleMethodOverride]
        return False

    def can_wipe_asset_partitions(self) -> bool:
        return False

    def test_filesystem_event_log_storage_run_corrupted(self, storage):
        # URL begins sqlite:///

        with open(
            os.path.abspath(storage.conn_string_for_shard("foo")[10:]), "w", encoding="utf8"
        ) as fd:
            fd.write("some nonsense")
        with pytest.raises(sqlalchemy.exc.DatabaseError):  # pyright: ignore[reportAttributeAccessIssue]
            storage.get_logs_for_run("foo")

    def test_filesystem_event_log_storage_run_corrupted_bad_data(self, storage):
        run_id_1, run_id_2 = [make_new_run_id() for _ in range(2)]
        SqlEventLogStorageMetadata.create_all(
            create_engine(storage.conn_string_for_shard(run_id_1))
        )
        with storage.run_connection(run_id_1) as conn:
            event_insert = SqlEventLogStorageTable.insert().values(
                run_id=run_id_1, event="{bar}", dagster_event_type=None, timestamp=None
            )
            conn.execute(event_insert)

        with pytest.raises(dg.DagsterEventLogInvalidForRun):
            storage.get_logs_for_run(run_id_1)

        SqlEventLogStorageMetadata.create_all(
            create_engine(storage.conn_string_for_shard(run_id_2))
        )

        with storage.run_connection(run_id_2) as conn:
            event_insert = SqlEventLogStorageTable.insert().values(
                run_id=run_id_2, event="3", dagster_event_type=None, timestamp=None
            )
            conn.execute(event_insert)
        with pytest.raises(dg.DagsterEventLogInvalidForRun):
            storage.get_logs_for_run(run_id_2)

    def cmd(self, exceptions, tmpdir_path):
        storage = SqliteEventLogStorage(tmpdir_path)
        try:
            storage.get_logs_for_run("foo")
        except Exception as exc:
            exceptions.put(exc)
            exc_info = sys.exc_info()
            traceback.print_tb(exc_info[2])

    def test_concurrent_sqlite_event_log_connections(self, storage):
        tmpdir_path = storage._base_dir  # noqa: SLF001
        ctx = multiprocessing.get_context("spawn")
        exceptions = ctx.Queue()
        ps = []
        for _ in range(5):
            ps.append(ctx.Process(target=self.cmd, args=(exceptions, tmpdir_path)))
        for p in ps:
            p.start()

        j = 0
        for p in ps:
            p.join()
            j += 1

        assert j == 5

        excs = []
        while not exceptions.empty():
            excs.append(exceptions.get())
        assert not excs, excs


class TestConsolidatedSqliteEventLogStorage(TestEventLogStorage):
    __test__ = True

    @pytest.fixture(name="instance", scope="function")
    def instance(self):  # pyright: ignore[reportIncompatibleMethodOverride]
        with tempfile.TemporaryDirectory(dir=os.getcwd()) as tmpdir_path:
            with dg.instance_for_test(
                temp_dir=tmpdir_path,
                overrides={
                    "event_log_storage": {
                        "module": "dagster.core.storage.event_log",
                        "class": "ConsolidatedSqliteEventLogStorage",
                        "config": {"base_dir": tmpdir_path},
                    }
                },
            ) as instance:
                yield instance

    @pytest.fixture(scope="function", name="storage")
    def event_log_storage(self, instance):  # pyright: ignore[reportIncompatibleMethodOverride]
        event_log_storage = instance.event_log_storage
        assert isinstance(event_log_storage, ConsolidatedSqliteEventLogStorage)
        yield event_log_storage

    def supports_multiple_event_type_queries(self):  # pyright: ignore[reportIncompatibleMethodOverride]
        return False

    def can_wipe_asset_partitions(self) -> bool:
        return False


class TestLegacyStorage(TestEventLogStorage):
    __test__ = True

    @pytest.fixture(name="instance", scope="function")
    def instance(self):  # pyright: ignore[reportIncompatibleMethodOverride]
        with tempfile.TemporaryDirectory(dir=os.getcwd()) as tmpdir_path:
            with dg.instance_for_test(temp_dir=tmpdir_path) as instance:
                yield instance

    @pytest.fixture(scope="function", name="storage")
    def event_log_storage(self, instance):  # pyright: ignore[reportIncompatibleMethodOverride]
        storage = instance.get_ref().storage
        assert isinstance(storage, DagsterSqliteStorage)
        legacy_storage = LegacyEventLogStorage(storage)
        legacy_storage.register_instance(instance)
        try:
            yield legacy_storage
        finally:
            legacy_storage.dispose()

    def can_wipe_asset_partitions(self) -> bool:
        return False

    def is_sqlite(self, storage):
        return True

    def supports_multiple_event_type_queries(self):  # pyright: ignore[reportIncompatibleMethodOverride]
        return False

    @pytest.mark.parametrize("dagster_event_type", ["dummy"])
    def test_get_latest_tags_by_partition(self, storage, instance, dagster_event_type):
        pytest.skip("skip this since legacy storage is harder to mock.patch")


def _insert_slots(conn: Connection, concurrency_key: str, num: int, delete_num: int = 0):
    rows = [
        {
            "concurrency_key": concurrency_key,
            "run_id": None,
            "step_key": None,
            "deleted": False,
        }
        for _ in range(0, num)
    ] + [
        {
            "concurrency_key": concurrency_key,
            "run_id": None,
            "step_key": None,
            "deleted": True,
        }
        for _ in range(0, delete_num)
    ]

    conn.execute(ConcurrencySlotsTable.insert().values(rows))


def _get_slot_count(conn: Connection, concurrency_key: str):
    slot_row = conn.execute(
        db_select([db.func.count(ConcurrencySlotsTable.c.id)]).where(
            db.and_(
                ConcurrencySlotsTable.c.concurrency_key == concurrency_key,
                ConcurrencySlotsTable.c.deleted == False,  # noqa: E712
            )
        )
    ).fetchone()
    return slot_row[0] if slot_row else None


def _get_limit_row_num(conn: Connection, concurrency_key: str):
    limit_row = conn.execute(
        db_select([ConcurrencyLimitsTable.c.limit]).where(
            ConcurrencyLimitsTable.c.concurrency_key == concurrency_key
        )
    ).fetchone()
    return limit_row[0] if limit_row else None


def test_concurrency_limit_set():
    """Test that the concurrency limit is set correctly.  This doesn't really belong in the event
    log test suites, since it kind of pokes at the table internals, by querying the limit rows in
    addition to the slot rows.
    """
    TOTAL_TIMEOUT_TIME = 30
    with tempfile.TemporaryDirectory(dir=os.getcwd()) as tmpdir_path:
        storage = ConcurrencyEnabledSqliteTestEventLogStorage(base_dir=tmpdir_path)

        def _allocate_slot():
            storage.set_concurrency_slots("foo", 5)

        start = time.time()
        with ThreadPoolExecutor() as executor:
            futures = [executor.submit(_allocate_slot) for i in range(100)]
            while not all(f.done() for f in futures) and time.time() < start + TOTAL_TIMEOUT_TIME:
                time.sleep(1)

        # assert that the number of slots match the limit row
        with storage.index_connection() as conn:
            assert _get_slot_count(conn, "foo") == 5
            assert _get_limit_row_num(conn, "foo") == 5


def test_concurrency_reconcile():
    """Test that the concurrency limit is set correctly.  This doesn't really belong in the event
    log test suites, since it kind of pokes at the table internals, by querying the limit rows in
    addition to the slot rows.
    """
    with tempfile.TemporaryDirectory(dir=os.getcwd()) as tmpdir_path:
        storage = ConcurrencyEnabledSqliteTestEventLogStorage(base_dir=tmpdir_path)

        # first set up the rows based on slots
        with storage.index_connection() as conn:
            _insert_slots(conn, "foo", 5, 1)
            _insert_slots(conn, "bar", 3, 2)

            assert _get_slot_count(conn, "foo") == 5
            assert _get_slot_count(conn, "bar") == 3
            assert _get_limit_row_num(conn, "foo") is None
            assert _get_limit_row_num(conn, "bar") is None

        storage._reconcile_concurrency_limits_from_slots()  # noqa: SLF001

        with storage.index_connection() as conn:
            assert _get_slot_count(conn, "foo") == 5
            assert _get_slot_count(conn, "bar") == 3
            assert _get_limit_row_num(conn, "foo") == 5
            assert _get_limit_row_num(conn, "bar") == 3


def test_run_stats():
    @dg.op
    def op_success(_):
        return 1

    @dg.op
    def asset_op(_):
        yield dg.AssetMaterialization(asset_key=dg.AssetKey("asset_1"))
        yield dg.Output(1)

    @dg.op
    def op_failure(_):
        raise ValueError("failing")

    def _ops():
        op_success()
        asset_op()
        op_failure()

    events, result = _synthesize_events(_ops, check_success=False)

    run_stats = build_run_stats_from_events(result.run_id, events)

    assert run_stats.run_id == result.run_id
    assert run_stats.materializations == 1
    assert run_stats.steps_succeeded == 2
    assert run_stats.steps_failed == 1
    assert (
        run_stats.start_time is not None
        and run_stats.end_time is not None
        and run_stats.end_time > run_stats.start_time
    )

    # build up run stats through incremental events
    incremental_run_stats = None
    for event in events:
        incremental_run_stats = build_run_stats_from_events(
            result.run_id, [event], incremental_run_stats
        )

    assert incremental_run_stats == run_stats


def test_step_stats():
    @dg.op
    def op_success(_):
        return 1

    @dg.op
    def asset_op(_):
        yield dg.AssetMaterialization(asset_key=dg.AssetKey("asset_1"))
        yield dg.Output(1)

    @dg.op(out=dg.Out(str))
    def op_failure(_):
        time.sleep(0.001)
        raise dg.RetryRequested(max_retries=3)

    def _ops():
        op_success()
        asset_op()
        op_failure()

    events, result = _synthesize_events(_ops, check_success=False)

    step_stats = build_run_step_stats_from_events(result.run_id, events)
    assert len(step_stats) == 3
    assert len([step for step in step_stats if step.status == StepEventStatus.SUCCESS]) == 2
    assert len([step for step in step_stats if step.status == StepEventStatus.FAILURE]) == 1
    assert all([step.run_id == result.run_id for step in step_stats])

    op_failure_stats = next(
        iter([step for step in step_stats if step.step_key == "op_failure"]), None
    )
    assert op_failure_stats
    assert op_failure_stats.attempts == 4
    assert len(op_failure_stats.attempts_list) == 4

    # build up run stats through incremental events
    incremental_snapshot = None
    for event in events:
        incremental_snapshot = build_run_step_stats_snapshot_from_events(
            result.run_id, [event], incremental_snapshot
        )

    assert incremental_snapshot
    assert incremental_snapshot.step_key_stats == step_stats


def test_step_worker_failure_attempts():
    run_id = "run_id"
    step_key = "step_key"
    job_name = "job_name"

    STEP_EVENT_LOGS = [
        dg.EventLogEntry(
            error_info=None,
            level=10,
            user_message="",
            run_id=run_id,
            timestamp=1738790993.7522137,
            step_key=step_key,
            job_name=job_name,
            dagster_event=dg.DagsterEvent(
                event_type_value="STEP_WORKER_STARTING",
                job_name=job_name,
                event_specific_data=EngineEventData(marker_start="step_process_start"),
                step_key=step_key,
            ),
        ),
        dg.EventLogEntry(
            error_info=None,
            level=10,
            user_message="",
            run_id=run_id,
            timestamp=1738791010.1940305,
            step_key=step_key,
            job_name=job_name,
            dagster_event=dg.DagsterEvent(
                event_type_value="STEP_UP_FOR_RETRY",
                job_name=job_name,
                event_specific_data=StepRetryData(
                    error=SerializableErrorInfo(message="", stack=[], cls_name=None),
                ),
                step_key=step_key,
            ),
        ),
        dg.EventLogEntry(
            error_info=None,
            level=10,
            user_message="",
            run_id=run_id,
            timestamp=1738791036.962399,
            step_key=step_key,
            job_name=job_name,
            dagster_event=dg.DagsterEvent(
                event_type_value="STEP_WORKER_STARTING",
                job_name=job_name,
                event_specific_data=EngineEventData(marker_start="step_process_start"),
                step_key=step_key,
            ),
        ),
    ]

    run_step_stats = build_run_step_stats_from_events(run_id, STEP_EVENT_LOGS)
    assert len(run_step_stats) == 1
    step_stat = run_step_stats[0]
    assert step_stat.attempts == 1
