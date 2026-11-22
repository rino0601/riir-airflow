"""설치된 Airflow의 example_dags를 입력으로 쓰는 파싱 코퍼스 테스트.

경로는 하드코딩하지 않고 ``airflow.__file__``에서 찾는다. Airflow가 없으면 skip한다.
"""

import ast

import pytest

airflow = pytest.importorskip("airflow", reason="Apache Airflow가 필요합니다.")

from airflow.dag_processing.dagbag import DagBag  # noqa: E402

from ._compat import BASELINE_AIRFLOW_VERSION, EXAMPLE_DAGS_PATH  # noqa: E402

_SDK_IMPORT = "from airflow.sdk import"
_DECORATORS_IMPORT = "from airflow.decorators import"


@pytest.fixture(scope="module")
def example_dag_files():
    if not EXAMPLE_DAGS_PATH.is_dir():
        pytest.skip(f"example_dags를 찾지 못했습니다: {EXAMPLE_DAGS_PATH}")
    files = sorted(EXAMPLE_DAGS_PATH.rglob("*.py"))
    if not files:
        pytest.skip(f"example_dags가 비어 있습니다: {EXAMPLE_DAGS_PATH}")
    return files


def _require_baseline():
    if airflow.__version__ != BASELINE_AIRFLOW_VERSION:
        pytest.skip(
            f"example_dags 코퍼스 기준은 Airflow {BASELINE_AIRFLOW_VERSION}입니다. "
            f"현재 버전: {airflow.__version__}"
        )


def test_baseline_corpus_is_available(example_dag_files):
    _require_baseline()
    assert len(example_dag_files) == 60


def test_example_dags_parse_and_register_with_riir(example_dag_files, tmp_path_factory):
    _require_baseline()

    target = tmp_path_factory.mktemp("riir-example-dags") / "example_dags"
    # copystat은 UserLAnd의 proot 파일시스템에서 EINVAL을 내므로 내용만 복사한다.
    for source in EXAMPLE_DAGS_PATH.rglob("*"):
        destination = target / source.relative_to(EXAMPLE_DAGS_PATH)
        if source.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source.read_bytes())

    rewritten = 0
    for path in target.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        ast.parse(source, filename=str(path))
        riir_source = source.replace(
            _SDK_IMPORT, "from riir_airflow.sdk import"
        ).replace(_DECORATORS_IMPORT, "from riir_airflow.decorators import")
        if riir_source != source:
            rewritten += 1
            path.write_text(riir_source, encoding="utf-8")

    assert rewritten > 0, "riir shim으로 바꿀 공개 import를 찾지 못했습니다."

    apache_bag = DagBag(dag_folder=EXAMPLE_DAGS_PATH, collect_dags=True)
    riir_bag = DagBag(dag_folder=target, collect_dags=True)

    assert not apache_bag.import_errors
    assert not riir_bag.import_errors
    assert set(apache_bag.dags) == set(riir_bag.dags)
    assert len(riir_bag.dags) >= 50
