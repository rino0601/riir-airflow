from datetime import timedelta
from enum import Enum
from typing import Self, NewType, cast

import pendulum

from airflow import DAG
from airflow.decorators import dag, task
from airflow.utils.context import Context
from airflow.utils.log.logging_mixin import LoggingMixin
from airflow.models.variable import Variable
from airflow.models.param import Param
from airflow.utils.types import NOTSET
from pydantic import BaseModel, Field, computed_field
from pydantic_extra_types.pendulum_dt import DateTime
import math

TARGET_BUFFER_RATIO: float = 3.0
UPPER_BASE_UNIT_WON: int = 10_0000
START_DATE: DateTime = pendulum.datetime(2024, 8, 1, tz="Asia/Seoul")

BankAccountKey = NewType("BankAccountKey", str)


def ceil(x, s):
    return s * math.ceil(float(x) / s)


def field2params(model: BaseModel, field_name: str, default_value=None) -> Param:
    from pydantic_core import PydanticUndefined

    field_info = model.model_fields[field_name]
    value = default_value or field_info.get_default()
    value = NOTSET if value is PydanticUndefined else value
    match field_info:
        case field_info if field_info.annotation is BankAccountKey:
            return Param(
                value,
                type="string",
                title=field_info.title,
                description_md=field_info.description,
                enum=[key for key in LAST_STATEMENT.bank_accounts.keys()],
            )
        case field_info if field_info.annotation is int:
            return Param(
                value,
                type="integer",
                title=field_info.title,
                description_md=field_info.description,
            )
        case field_info if field_info.annotation is float:
            return Param(
                value,
                type="number",
                title=field_info.title,
                description_md=field_info.description,
            )
        case field_info if issubclass(field_info.annotation, Enum):
            return Param(
                value,
                type="string",
                title=field_info.title,
                description_md=field_info.description,
                enum=[a.value for a in field_info.annotation],
            )
        case field_info:
            return Param(
                value,
                type="string",
                title=field_info.title,
                description_md=field_info.description,
            )


class AFVarIoMixin:
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


class DagUpsertMixin(AFVarIoMixin):
    @property
    def key(self) -> str:
        raise NotImplementedError

    @classmethod
    def dag(cls, instance: Self | None = None) -> DAG:
        @task()
        def save(**kwargs):
            global LAST_STATEMENT
            circuit = MoneyCircuit()
            obj = cls(**kwargs["params"])
            LAST_STATEMENT = circuit.update(LAST_STATEMENT, obj)
            obj.set(obj.key, description=f"{kwargs['dag'].dag_id} 에서 upsert 된 값")
            LAST_STATEMENT = circuit.save(LAST_STATEMENT)

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
                if key not in ["key", "created_at", "modified_at"]
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
    modified_at: DateTime = Field(
        # 누가봐도 수정한적 없어보이게 하기 위해 아주 옛날시간을 기본값으로 삼음.
        default_factory=lambda: DateTime.now("Asia/Seoul").set(year=1900),
    )
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
    def key(self) -> BankAccountKey:
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
        description="""
        계좌와 계좌간 송금 정책을 결정합니다. top-up 들이 먼저 처리 됩니다.
        
        - overflow: 출금 계좌가 넘칠 때 송금합니다. 
        - top-up:  입금 계좌가 모자랄 때 송금합니다. 
        """,
    )
    input_account_key: BankAccountKey = Field(
        title="출금 계좌",
        description="돈이 빠져나갈 계좌의 key",
    )
    output_account_key: BankAccountKey = Field(
        title="입금 계좌",
        description="돈이 들어가야할 계좌의 key",
    )
    ratio: float = Field(
        1.0,
        title="작동 비율",
        description="""
        기준 계좌의 should_be_won 의 어느정도 수준일 때 작동할지 결정합니다. 일반적으로 아래의 값을 추천합니다.

        - overflow: 1.0  
        - top-up: 0.8 
        """,
    )

    @computed_field
    @property
    def key(self) -> str:
        return f"pipe.{self.name}.dag"


class MonthlyStatement(AFVarIoMixin, BaseModel):
    period: str  # %Y-%m
    phase: str = "draft"
    created_at: DateTime = Field(default_factory=lambda: DateTime.now("Asia/Seoul"))
    bank_accounts: dict[BankAccountKey, BankAccount] = {}
    flow_pipes: dict[str, MoneyFlowPipe] = {}

    @computed_field
    @property
    def key(self) -> str:
        return f"statement.{self.period}.json"


class MoneyCircuit(LoggingMixin):
    def update(self, stmt: MonthlyStatement, obj: DagUpsertMixin) -> MonthlyStatement:
        match obj:
            case BankAccount():
                self.log.info(f"save {obj.key}")
                cast(BankAccount, obj).modified_at = DateTime.now("Asia/Seoul")
                stmt.bank_accounts[obj.key] = obj
            case MoneyFlowPipe():
                self.log.info(f"save {obj.key}")
                stmt.flow_pipes[obj.key] = obj
            case _:
                self.log.warning("Doesn't match any")
        return stmt

    def get_statement(self, period: str) -> MonthlyStatement:
        key = f"statement.{period}.json"
        try:
            stmt = MonthlyStatement.get(key=key)
        except KeyError:
            stmt = MonthlyStatement(key=key, period=period)
        return stmt

    def draft_statment(self, context: Context) -> MonthlyStatement:
        """"""
        current = context["data_interval_end"].in_tz("Asia/Seoul")
        prev_stmt = self.get_statement(
            period=context["data_interval_start"].strftime("%Y-%m")
        )
        prev_stmt.created_at = current
        prev_stmt.period = current.strftime("%Y-%m")
        return self.save(prev_stmt)

    def save(self, stmt: MonthlyStatement) -> MonthlyStatement:
        stmt.set(
            key=stmt.key,
            description="MoneyCircuit가 생성한 결산서",
        )
        return MonthlyStatement.get(stmt.key)


LAST_STATEMENT = MoneyCircuit().get_statement(
    period=pendulum.now("Asia/Seoul").strftime("%Y-%m")
)

# Provider 구현까지 가야 한다. 이건 다음에 하자.
# class EditPageLink(BaseOperatorLink):
#     name = "Edit Account"
#
#     def get_link(self, operator: BaseOperator, *, ti_key: TaskInstanceKey) -> str:
#         return str(ti_key)


@dag(
    schedule="0 21 28 * *",  # 매 월 28일 밤 9시.
    start_date=START_DATE,
    catchup=False,
    tags=["example"],
)
def money_circuit():
    """ """
    from logging import getLogger

    log = getLogger("airflow.task")

    @task()
    def draft(**kwargs) -> list[BankAccountKey]:
        circuit = MoneyCircuit()
        stmt = circuit.draft_statment(context=kwargs)
        return [key for key in stmt.bank_accounts.keys()]

    @task(retries=99999, retry_delay=timedelta(seconds=10))
    def wait_for_user_update(bank_account_key, **kwargs):
        context: Context = kwargs
        circuit = MoneyCircuit()
        current = context["data_interval_end"].strftime("%Y-%m")
        stmt = circuit.get_statement(period=current)

        ba = BankAccount.get(bank_account_key)
        if ba.modified_at <= stmt.created_at:
            log.info(f"{bank_account_key} is {ba.modified_at=}. wait_for_retry!")
            raise ValueError("Try Again!")
        circuit.save(circuit.update(stmt, ba))
        log.info("waiting is done!")

    @task()
    def process_pipes(**kwargs):
        context: Context = kwargs
        circuit = MoneyCircuit()
        current = context["data_interval_end"].strftime("%Y-%m")
        stmt = circuit.get_statement(period=current)

        for pipe in stmt.flow_pipes.values():
            match pipe.kind:
                case PipeKind.overflow:
                    log.info(f"Hi Overflow {pipe.name}")
                case PipeKind.top_up:
                    log.info(f"Hi Top-UP {pipe.name}")
                case _:
                    log.warning("Doesn't match Any!")

    keys = draft()
    waiting = wait_for_user_update.expand(bank_account_key=keys)
    _ = waiting >> process_pipes()


money_circuit()
MoneyFlowPipe.dag()
BankAccount.dag()
for ba in LAST_STATEMENT.bank_accounts.values():
    ba.dag(ba)
for pipe in LAST_STATEMENT.flow_pipes.values():
    pipe.dag(pipe)
