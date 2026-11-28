import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from uuid import uuid4

import pytest


def _status(database, *, cwd=None):
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "riir_airflow",
            "scheduler-status",
            "--database",
            str(database),
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _migration_env():
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("AIRFLOW")
    }
    env.update(
        AIRFLOW__CORE__LOAD_EXAMPLES="False",
        AIRFLOW__CORE__EXECUTOR="LocalExecutor",
        AIRFLOW__DATABASE__SQL_ALCHEMY_CONN="sqlite:///placeholder.db",
    )
    return env


def _database_state(database):
    with closing(sqlite3.connect(database)) as db:
        tables = [
            name
            for (name,) in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        ]
        return (
            db.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "ORDER BY type, name"
            ).fetchall(),
            db.execute("SELECT version_num FROM alembic_version").fetchall(),
            tuple(
                (name, db.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0])
                for name in tables
            ),
            db.execute("PRAGMA integrity_check").fetchall(),
            db.execute("PRAGMA foreign_key_check").fetchall(),
        )


def test_migrated_database_copies_are_equivalent_and_isolated(
    tmp_path, migrated_db_factory
):
    env = _migration_env()
    first = migrated_db_factory.prepare(tmp_path / "first", env)
    second = migrated_db_factory.prepare(tmp_path / "second", env)
    fresh = migrated_db_factory.fresh(tmp_path / "fresh", env)
    configured_env = {**env, "AIRFLOW__CORE__PARALLELISM": "4"}
    configured = migrated_db_factory.prepare(tmp_path / "configured", configured_env)

    assert migrated_db_factory.template_for(env) == migrated_db_factory.template_for(
        {**env, "AIRFLOW__DATABASE__SQL_ALCHEMY_CONN": "sqlite:///elsewhere.db"}
    )
    assert migrated_db_factory.template_for(env) != migrated_db_factory.template_for(
        configured_env
    )
    assert (
        _database_state(first)
        == _database_state(second)
        == _database_state(configured)
        == _database_state(fresh)
    )

    with sqlite3.connect(first) as db:
        db.execute(
            "INSERT INTO dag_run (dag_id, run_id, run_type, run_after, state) "
            "VALUES ('probe', 'written', 'manual', CURRENT_TIMESTAMP, 'queued')"
        )
    with closing(sqlite3.connect(second)) as db:
        assert db.execute("SELECT count(*) FROM dag_run").fetchone() == (0,)
        assert db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]


def test_scheduler_status_reads_committed_airflow_state(tmp_path):
    database = tmp_path / "airflow metadata.db"
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("AIRFLOW")
    }
    env.update(
        AIRFLOW_HOME=str(tmp_path),
        AIRFLOW__CORE__LOAD_EXAMPLES="False",
        AIRFLOW__DATABASE__SQL_ALCHEMY_CONN=f"sqlite:///{database}",
    )
    migrate = subprocess.run(
        [sys.executable, "-m", "airflow", "db", "migrate"],
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert migrate.returncode == 0, migrate.stdout + migrate.stderr

    with closing(sqlite3.connect(database)) as db:
        with db:
            db.executemany(
                """INSERT INTO dag_run (dag_id, run_id, run_type, run_after, state)
                   VALUES ('probe', ?, 'manual', CURRENT_TIMESTAMP, ?)""",
                [("waiting", "queued"), ("active", "running"), ("done", "success")],
            )
            db.executemany(
                """INSERT INTO task_instance
                   (id, dag_id, run_id, task_id, state, working_set, try_number, pool, pool_slots)
                   VALUES (?, 'probe', 'active', ?, ?, ?, 1, 'default_pool', 1)""",
                [
                    (uuid4().hex, task_id, state, working_set)
                    for task_id, state, working_set in [
                        ("scheduled", "scheduled", 1),
                        ("queued", "queued", 1),
                        ("running", "running", 1),
                        ("retry", "up_for_retry", 1),
                        ("restart", "restarting", 1),
                        ("sensor", "up_for_reschedule", 1),
                        ("trigger", "deferred", 1),
                        ("human_input", "awaiting_input", 1),
                        ("no_state", None, 1),
                        ("archived", "running", 0),
                        ("archived_deferred", "deferred", None),
                        ("archived_no_state", None, None),
                        ("done", "success", 1),
                    ]
                ],
            )

        def read_status():
            result = _status(database)
            assert result.returncode == 0, result.stderr
            return json.loads(result.stdout)

        expected = {
            "dag_runs": {"queued": 1, "running": 1},
            "task_instances": {
                "scheduled": 1,
                "queued": 1,
                "running": 1,
                "up_for_retry": 1,
                "restarting": 1,
                "up_for_reschedule": 1,
                "deferred": 1,
                "awaiting_input": 1,
                "no_state": 1,
            },
        }
        with db:
            db.execute("UPDATE dag_run SET state = 'running' WHERE run_id = 'waiting'")
            db.execute(
                """UPDATE task_instance SET state = 'queued', try_number = 2
                   WHERE task_id = 'retry'"""
            )
            db.execute(
                "UPDATE task_instance SET state = 'scheduled' WHERE task_id = 'trigger'"
            )
            assert read_status() == expected
        expected["dag_runs"] = {"queued": 0, "running": 2}
        expected["task_instances"].update(
            scheduled=2, queued=2, up_for_retry=0, deferred=0
        )
        assert read_status() == expected


def test_scheduler_status_does_not_create_missing_database(tmp_path):
    database = tmp_path / "missing.db"
    result = _status(database)
    assert result.returncode == 1
    assert result.stdout == ""
    assert "Cannot read scheduler status" in result.stderr
    assert not database.exists()


@pytest.mark.skipif(os.name != "posix", reason="requires ':' in a filename")
def test_scheduler_status_reads_literal_relative_filename(tmp_path):
    filename = "file:metadata.db"
    for path in (tmp_path / filename, tmp_path / "metadata.db"):
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE dag_run (state)")
            db.execute("CREATE TABLE task_instance (state, working_set)")
            if path.name == filename:
                db.execute("INSERT INTO dag_run VALUES ('queued')")

    result = _status(filename, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["dag_runs"] == {"queued": 1, "running": 0}
