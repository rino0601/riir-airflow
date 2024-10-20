from enum import Enum
from typing import Self

import pendulum
from pendulum.datetime import DateTime

from airflow import DAG
from airflow.decorators import dag, task
from airflow.utils.context import Context
from airflow.utils.log.logging_mixin import LoggingMixin
from airflow.models.variable import Variable
from airflow.models.param import Param
from airflow.utils.types import NOTSET
from pydantic import BaseModel, Field, computed_field
import math

TARGET_BUFFER_RATIO: float = 3.0
UPPER_BASE_UNIT_WON: int = 10_0000
START_DATE: DateTime = pendulum.datetime(2024, 8, 1, tz="Asia/Seoul")


def ceil(x, s):
    return s * math.ceil(float(x) / s)


def field2params(model: BaseModel, field_name: str, default_value=None) -> Param:
    value = default_value or NOTSET
    match model.model_fields[field_name]:
        case field_info if field_info.annotation is int:
            return Param(
                value,
                type="integer",
                title=field_info.title,
                description=field_info.description,
            )
        case field_info if field_info.annotation is float:
            return Param(
                value,
                type="number",
                title=field_info.title,
                description=field_info.description,
            )
        case field_info if issubclass(field_info.annotation, Enum):
            return Param(
                value,
                type="string",
                title=field_info.title,
                description=field_info.description,
                enum=[a.value for a in field_info.annotation],
            )
        case field_info:
            return Param(
                value,
                type="string",
                title=field_info.title,
                description=field_info.description,
            )


class DagUpsertMixin:
    @property
    def key(self) -> str:
        raise NotImplementedError

    @classmethod
    def dag(cls, instance: Self | None = None) -> DAG:
        @task()
        def save(**kwargs):
            circuit = MoneyCircuit()
            obj = cls(**kwargs["params"])
            stmt = circuit.get_statement(
                period=pendulum.now("Asia/Seoul").strftime("%Y-%m")
            )
            circuit.update(stmt, cls, obj)
            circuit.save(stmt)

        with DAG(
            dag_id=instance.key if instance else f"Create.{cls.__name__}",
            schedule=None,  # 수동 트리거 전용
            start_date=START_DATE,
            catchup=False,
            params={
                key: field2params(
                    model=cls,
                    field_name=key,
                    default_value=getattr(instance, key) if instance else None,
                )
                for key in cls.model_fields
                if key not in ["key"]
            },
        ) as dag:
            save()

        return dag


class BankAccountPurpose(str, Enum):
    income = "income"
    spend = "spend"
    sink = "sink"
    loan = "loan"


class BankAccount(DagUpsertMixin, BaseModel):
    name: str = Field(
        title="계좌 이름",
        description="은행 계좌 이름. 구분할 수 있게 작성하세요",
    )
    purpose: BankAccountPurpose = Field(
        title="계좌 목적",
        description="계좌의 목적입니다. income 은 대표 수입 계좌, spend 는 대부분의 지불계좌, sink 는 남은 잔액들이 모일 계좌를 뜻 합니다.",
    )
    current_amount_won: int = Field(
        title="현재 잔액",
        description="현재 은행에 남아 있는 잔액을 입력합니다.",
    )
    expected_cost_won: int = Field(
        title="예상 지출액",
        description="이번달에 결제될 예상 금액을 입력합니다.",
    )
    target_buffer_ratio: float = Field(
        TARGET_BUFFER_RATIO,
        title="목표 예비율",
        description="계좌에 몇 개월치를 예비해 둘 것인지 설정합니다.",
    )
    upper_base_unit_won: int = Field(
        UPPER_BASE_UNIT_WON,
        title="반올림 기준",
        description="반올림 단위를 결정 합니다.",
    )

    @computed_field
    @property
    def should_be_won(self) -> int:
        """계좌에 있어야 하는 금액입니다."""
        required_won = self.expected_cost_won * self.target_buffer_ratio
        return ceil(required_won, self.upper_base_unit_won)

    @computed_field
    @property
    def key(self) -> str:
        return f"bank_account.{self.name}.dag"


class PipeKind(str, Enum):
    overflow = "overflow"
    top_up = "top-up"


class MoneyFlowPipe(DagUpsertMixin, BaseModel):
    name: str = Field(
        title="이름",
        description="계좌 이체 규칙 이름입니다. 구분가능하게 중복되지 않게 작명해주세요.",
    )
    kind: PipeKind = Field(
        title="파이프 유형",
        description="""계좌와 계좌간 송금 정책을 결정합니다. 
        
        - overflow: 출금 계좌가 넘칠 때 송금합니다. 
        - top-up:  입금 계좌가 모자랄 때 송금합니다. overflow 들이 먼저 처리 됩니다.
        """,
    )
    input_account_key: str = Field(
        title="출금 계좌",
        description="돈이 빠져나갈 계좌의 key",
    )
    output_account_key: str = Field(
        title="입금 계좌",
        description="돈이 들어가야할 계좌의 key",
    )
    ratio: float = Field(
        0.8,
        title="작동 비율",
        description="""기준 계좌의 should_be_won 의 어느정도 수준일 때 작동할지 결정합니다. 일반적으로 아래의 값을 추천합니다.

        - overflow: 1.0  
        - top-up: 0.8 
        """,
    )

    @computed_field
    @property
    def key(self) -> str:
        return f"pipe.{self.name}.dag"


class MonthlyStatement(BaseModel):
    key: str  # monthly_statement.%Y-%m
    period: str  # %Y-%m
    phase: str = "draft"
    bank_accounts: dict[str, BankAccount] = {}
    flow_pipes: dict[str, MoneyFlowPipe] = {}

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


class MoneyCircuit(LoggingMixin):
    def update(self, stmt: MonthlyStatement, model: BaseModel, obj: DagUpsertMixin):
        match model:
            case _ if isinstance(model, BankAccount):
                stmt.bank_accounts[obj.key] = obj
            case _ if isinstance(model, MoneyFlowPipe):
                stmt.flow_pipes[obj.key] = obj

    def get_statement(self, period: str) -> MonthlyStatement:
        key = f"statement.{period}.json"
        try:
            stmt = MonthlyStatement.get(key=key)
        except KeyError:
            stmt = MonthlyStatement(key=key, period=period)
        return stmt

    def draft_statment(self, context: Context) -> MonthlyStatement:
        """"""
        current = context["data_interval_end"].strftime("%Y-%m")
        last_stmt = self.get_statement(
            period=context["data_interval_start"].strftime("%Y-%m")
        )
        stmt = MonthlyStatement(
            key=f"statement.{current}.json",
            period=current,
            bank_accounts=last_stmt.bank_accounts,
        )
        return self.save(stmt)

    def save(self, stmt: MonthlyStatement) -> MonthlyStatement:
        stmt.set(
            key=stmt.key,
            description="MoneyCircuit가 생성한 결산서",
        )
        return stmt


LAST_STATEMENT = MoneyCircuit().get_statement(
    period=pendulum.now("Asia/Seoul").strftime("%Y-%m")
)


@dag(
    schedule="0 21 28 * *",  # 매 월 28일 밤 9시.
    start_date=START_DATE,
    catchup=False,
    tags=["example"],
)
def money_circuit():
    """ """

    @task()
    def extract(**kwargs) -> dict:
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
MoneyFlowPipe.dag()
BankAccount.dag()
for ba in LAST_STATEMENT.bank_accounts.values():
    ba.dag(ba)
