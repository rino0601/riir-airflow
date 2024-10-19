from typing import Self

import pendulum

from airflow.decorators import dag, task
from airflow.utils.context import Context
from airflow.utils.log.logging_mixin import LoggingMixin
from airflow.models.variable import Variable
from pydantic import BaseModel

TARGET_BUFFER_RATIO: float = 3.0
UPPER_BASE_UINT_WON: int = 10_0000


class AFVarIoModel(BaseModel):
    @classmethod
    def get(cls, key: str) -> Self:
        return cls.model_validate_json(Variable.get(key))

    def set(self, key: str, description: str):
        Variable.set(
            key=key,
            value=self.model_dump_json(indent=2),
            description=description,
            serialize_json=False,  # pydantic이 이미 처리 함.
        )


class BankAccount(AFVarIoModel):
    amount_won: int
    expected_cost_won: int
    target_buffer_ratio: float = TARGET_BUFFER_RATIO
    upper_base_unit_won: int = UPPER_BASE_UINT_WON


class MonthlyStatement(AFVarIoModel):
    key: str
    period: str
    phase: str = "draft"
    bank_accounts: dict[str, BankAccount] = {
        "sample": BankAccount(
            amount_won=0,
            expected_cost_won=20000,
        )
    }


class MoneyCircuit(LoggingMixin):
    def draft_statment(self, context: Context) -> str:
        """"""
        prev = context["data_interval_start"].strftime("%Y-%m")
        current = context["data_interval_end"].strftime("%Y-%m")
        try:
            before_stmt = MonthlyStatement.get(key=f"statement.{prev}.json")
        except KeyError:
            before_stmt = MonthlyStatement(key="dummy", period="dummy")
        stmt = MonthlyStatement(
            key=f"statement.{current}.json",
            period=current,
            bank_accounts=before_stmt.bank_accounts,
        )
        stmt.set(
            key=stmt.key,
            description="MoneyCircuit가 생성한 결산서",
        )
        return stmt.key


@dag(
    schedule="0 21 28 * *",  # 매 월 28일 밤 9시.
    start_date=pendulum.datetime(2024, 8, 1, tz="Asia/Seoul"),
    catchup=False,
    tags=["example"],
)
def money_circuit():
    """
    ### TaskFlow API Tutorial Documentation

    This is a simple data pipeline example which demonstrates the use of
    the TaskFlow API using three simple tasks for Extract, Transform, and Load.
    Documentation that goes along with the Airflow TaskFlow API tutorial is
    located [here](https://airflow.apache.org/docs/apache-airflow/stable/tutorial_taskflow_api.html)
    """

    @task()
    def extract(**kwargs) -> dict:
        """\
        #### Extract task
        A simple Extract task to get data ready for the rest of the data
        pipeline. In this case, getting data is simulated by reading from a
        hardcoded JSON string.
        """
        circuit = MoneyCircuit()
        circuit.draft_statment(context=kwargs)

        return {"a": 1, "b": 2}

    @task(multiple_outputs=True)
    def transform(order_data_dict: dict):
        """

        #### Transform task

        A simple Transform task which takes in the collection of order data and
        computes the total order value.
        """
        total_order_value = 0

        for value in order_data_dict.values():
            total_order_value += value

        return {"total_order_value": total_order_value}

    @task()
    def load(total_order_value: float):
        """
        #### Load task
        A simple Load task which takes in the result of the Transform task and
        instead of saving it to end user review, just prints it out.
        """

        print(f"Total order value is: {total_order_value:.2f}")

    order_data = extract()
    order_summary = transform(order_data)
    load(order_summary["total_order_value"])


money_circuit()
