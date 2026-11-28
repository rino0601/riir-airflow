"""Compare native SQLite scheduler claims with the Airflow 3.4 baseline."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.differential


@pytest.mark.parametrize(
    "mode",
    ["native", "mixed-native", "mixed-newer-native"],
)
def test_sqlite_scheduler_admits_only_one_queued_run(admission_probe, mode):
    admission_probe(["admission_probe.py"], "scheduler_admission", mode)


@pytest.mark.parametrize("mode", ["native"])
def test_sqlite_scheduler_creates_and_starts_scheduled_run(admission_probe, mode):
    admission_probe(["schedule_probe.py"], "scheduled_runs", mode)


def test_native_scheduler_delegates_unsupported_scheduled_creation(admission_probe):
    admission_probe(["schedule_probe.py"], "unsupported_schedule_creation", "native")


def test_native_scheduler_delegates_explicit_executor_tasks(admission_probe):
    admission_probe(["schedule_executor_probe.py"], "schedule_executor_tasks", "native")


@pytest.mark.parametrize("mode", ["native"])
def test_scheduled_run_honors_disabled_bundle_versioning(admission_probe, mode):
    admission_probe(
        ["schedule_disabled_version_probe.py"], "disabled_bundle_versioning", mode
    )


@pytest.mark.parametrize("mode", ["native"])
def test_sqlite_scheduler_skips_capacity_blocked_dags_before_limit(
    admission_probe, mode
):
    admission_probe(
        ["capacity_probe.py"],
        "capacity_blocked_dags",
        mode,
        AIRFLOW__SCHEDULER__MAX_DAGRUNS_TO_CREATE_PER_LOOP="1",
    )


def test_native_scheduler_delegates_pending_asset_work(admission_probe):
    admission_probe(["schedule_probe.py"], "pending_asset_work", "native")


@pytest.mark.parametrize("mode", ["native", "fallback"])
def test_sqlite_scheduler_admits_task_attempts(admission_probe, mode):
    admission_probe(
        ["task_selection_probe.py"],
        "task_attempts",
        mode,
        AIRFLOW__CORE__PARALLELISM="4" if mode == "native" else "1",
        AIRFLOW__SCHEDULER__MAX_TIS_PER_QUERY="1",
    )


def test_native_scheduler_transaction_boundaries(admission_probe):
    admission_probe(["admission_probe.py"], "transaction_boundaries", "native")


def test_native_scheduler_fallback_before_write(admission_probe):
    admission_probe(["admission_probe.py"], "fallback_before_write", "native")


def test_native_scheduler_delegates_with_listeners(admission_probe):
    admission_probe(["admission_probe.py"], "listeners_delegation", "native")


def test_scheduler_entry_point_runner_calls_native_claims(admission_probe):
    admission_probe(["entrypoint_probe.py"], "runner_native_claims", "native")


def test_python_module_scheduler_entry_point(admission_probe):
    admission_probe(
        ["admission_probe.py"],
        "python_module_entry_point",
        "native",
        AIRFLOW__SCHEDULER__NUM_RUNS="1",
        AIRFLOW__CORE__UNIT_TEST_MODE="True",
    )


@pytest.mark.parametrize(
    "mode",
    ["supported-native", "fallback-native"],
)
def test_native_scheduler_evaluates_task_dependencies(admission_probe, mode):
    dag = (
        "dependency_fallback_probe.py"
        if mode.startswith("fallback")
        else "dependency_probe.py"
    )
    admission_probe([dag], "dependency_evaluation", mode)


@pytest.mark.parametrize("mode", ["future-native"])
def test_native_scheduler_defers_future_logical_dates(admission_probe, mode):
    admission_probe(["dependency_probe.py"], "future_logical_dates", mode)


@pytest.mark.parametrize("mode", ["version-native"])
def test_native_scheduler_delegates_changed_unpinned_runs(admission_probe, mode):
    admission_probe(
        ["dependency_probe.py"],
        "changed_unpinned_runs",
        mode,
        fresh_parse=True,
    )


@pytest.mark.parametrize("mode", ["timeout-native"])
def test_native_scheduler_delegates_dagrun_timeouts(admission_probe, mode):
    admission_probe(["timeout_probe.py"], "dagrun_timeouts", mode)


@pytest.mark.parametrize("mode", ["retry-native"])
def test_native_scheduler_honors_retry_period(admission_probe, mode):
    admission_probe(["dependency_probe.py"], "retry_period", mode)


@pytest.mark.parametrize(
    "mode",
    [
        f"{case}-native"
        for case in ("success", "failure", "unfinished", "retry", "callback")
    ],
)
def test_native_scheduler_finalizes_task_outcomes(admission_probe, mode):
    dag = "callback_probe.py" if mode.startswith("callback") else "dependency_probe.py"
    admission_probe([dag], "finalized_task_outcomes", mode)


@pytest.mark.parametrize("mode", ["mixed-native"])
def test_native_scheduler_phase_handoff_is_explicit(admission_probe, mode):
    admission_probe(["phase_handoff_probe.py"], "phase_handoff", mode)


def test_database_path_keeps_relative_sqlite_url(monkeypatch):
    from airflow.configuration import conf
    from riir_airflow.scheduler import _database_path

    monkeypatch.setattr(conf, "get", lambda *args, **kwargs: "sqlite:///airflow.db")
    assert _database_path() == Path("airflow.db")
    monkeypatch.setattr(
        conf, "get", lambda *args, **kwargs: "sqlite:////tmp/airflow.db"
    )
    assert _database_path() == Path("/tmp/airflow.db")
