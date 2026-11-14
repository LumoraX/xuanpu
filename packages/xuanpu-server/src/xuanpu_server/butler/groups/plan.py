"""编组计划与编组记录的数据模型，以及推进时用到的纯函数。

计划是声明式数据，不是脚本：几步，每步并行或接力，每步的成员（匠人、任务说明、材料），
以及可选的 ``when``（只认前一步的终态）。未知字段一律拒绝。
会诊（consult）：成员各自就同一问题给结论、互不可见；全部完成后比对分歧再裁决（R5-D1）。
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime  # noqa: TC003 —— pydantic 在运行时解析字段类型
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from xuanpu_server.butler.groups.verdict import (
    StepVerdict,  # noqa: TC001 —— pydantic 在运行时解析字段类型
)
from xuanpu_server.butler.models import INSTRUCTION_MAX
from xuanpu_server.work_tasks.models import (
    InputFile,  # noqa: TC001 —— pydantic 在运行时解析字段类型
)

StepMode = Literal["parallel", "relay", "consult"]
StepWhen = Literal["completed", "failed"]
MemberOutcome = Literal["completed", "failed", "cancelled"]
StepOutcome = Literal["completed", "failed", "skipped", "cancelled"]
GroupState = Literal["running", "completed", "failed", "cancelled"]
GROUP_TERMINAL: frozenset[str] = frozenset({"completed", "failed", "cancelled"})
TITLE_MAX = 120
SUMMARY_MAX = 1500
_ID = re.compile(r"^[A-Za-z0-9_.:@/-]{1,160}$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class GroupMember(BaseModel):
    """计划里的一个成员：哪位匠人、交给它的说明、本人明确提供的材料。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    package: str
    entry: str
    brief: str = Field(min_length=1, max_length=INSTRUCTION_MAX)
    materials: tuple[InputFile, ...] = Field(default=(), max_length=8)

    @field_validator("package", "entry")
    @classmethod
    def _name(cls, value: str) -> str:
        """匠人引用只能是包名与入口名，不能夹带其他内容。"""
        if not _NAME.fullmatch(value):
            raise ValueError("匠人引用格式不对")
        return value

    @field_validator("brief")
    @classmethod
    def _brief(cls, value: str) -> str:
        """说明去掉首尾空白后不能为空。"""
        if not value.strip():
            raise ValueError("任务说明不能为空")
        return value.strip()


class GroupStep(BaseModel):
    """计划的一步。``when`` 为空时前一步结束即进行；否则只在前一步是该终态时进行。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    mode: StepMode
    members: tuple[GroupMember, ...] = Field(min_length=1)
    when: StepWhen | None = None


class GroupPlan(BaseModel):
    """一份编组计划；固定后不可改，调整须另交新计划。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    title: str = Field(min_length=1, max_length=TITLE_MAX)
    steps: tuple[GroupStep, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _first_step_unconditional(self) -> GroupPlan:
        """第一步没有「前一步」，不能带 ``when``。"""
        if self.steps[0].when is not None:
            raise ValueError("第一步不能带 when")
        return self

    def member_count(self) -> int:
        """全部成员数（每个成员是一次派工）。"""
        return sum(len(step.members) for step in self.steps)


class MemberRun(BaseModel):
    """一个成员的执行：对应的派工、结果与给员工看的摘要。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    step: int = Field(ge=0)
    index: int = Field(ge=0)
    dispatch_id: str | None = None
    outcome: MemberOutcome | None = None
    end_reason: str | None = None
    summary: str | None = Field(default=None, max_length=SUMMARY_MAX + 1)
    passed: bool | None = None


class FollowUp(BaseModel):
    """会诊里的一次定向追问（R5-D2）：哪条消息问了哪位成员、为此派出的派工与结果。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    step: int = Field(ge=0)
    target: int = Field(ge=0)
    asked_by: int = Field(ge=1, description="提问消息的序号")
    round: int = Field(ge=1)
    dispatch_id: str | None = None
    outcome: MemberOutcome | None = None
    end_reason: str | None = None
    summary: str | None = Field(default=None, max_length=SUMMARY_MAX + 1)
    passed: bool | None = None


class GroupRecord(BaseModel):
    """一个编组。``version`` 每次保存加一，保存须带期望版本。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    group_id: str
    tenant_id: str
    user_id: str
    request_id: str = Field(min_length=1, max_length=128)
    input_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    trace_id: str
    parent_span_id: str | None = None
    timezone: str = "Asia/Shanghai"
    plan: GroupPlan
    state: GroupState = "running"
    step: int = Field(default=0, ge=0)
    step_outcomes: tuple[StepOutcome, ...] = ()
    runs: tuple[MemberRun, ...] = ()
    cancel_requested: bool = False
    summarized: bool = False
    verdicts: tuple[StepVerdict, ...] = ()
    followups: tuple[FollowUp, ...] = ()
    version: int = Field(default=1, ge=1)
    created_at: datetime
    updated_at: datetime

    @field_validator("group_id", "tenant_id", "user_id", "trace_id", "parent_span_id")
    @classmethod
    def _ids(cls, value: str | None) -> str | None:
        """标识走有限字符集。"""
        if value is not None and not _ID.fullmatch(value):
            raise ValueError("标识格式不对")
        return value

    @property
    def active(self) -> bool:
        """还没收口（未到终态或终态后尚未汇总）。"""
        return self.state not in GROUP_TERMINAL or not self.summarized

    def run(self, step: int, index: int) -> MemberRun | None:
        """某步某成员的执行；还没启动返回 None。"""
        return next((r for r in self.runs if (r.step, r.index) == (step, index)), None)

    def verdict(self, step: int) -> StepVerdict | None:
        """某步会诊的裁决；还没比对返回 None。"""
        return next((v for v in self.verdicts if v.step == step), None)

    def by_dispatch(self, dispatch_id: str) -> MemberRun | None:
        """按派工找成员。"""
        return next((r for r in self.runs if r.dispatch_id == dispatch_id), None)

    def followup(self, asked_by: int, target: int) -> FollowUp | None:
        """某条消息对某位成员的追问；还没派出返回 None。"""
        return next((f for f in self.followups if (f.asked_by, f.target) == (asked_by, target)),
                    None)

    def followup_by_dispatch(self, dispatch_id: str) -> FollowUp | None:
        """按派工找追问。"""
        return next((f for f in self.followups if f.dispatch_id == dispatch_id), None)


def group_fingerprint(trace_id: str, plan: GroupPlan) -> str:
    """同一 ``request_id`` 的编组内容指纹。"""
    body = trace_id + "\n" + plan.model_dump_json()
    return hashlib.sha256(body.encode()).hexdigest()


def with_run(record: GroupRecord, run: MemberRun) -> GroupRecord:
    """替换或追加一个成员的执行。"""
    others = tuple(r for r in record.runs if (r.step, r.index) != (run.step, run.index))
    runs = tuple(sorted((*others, run), key=lambda r: (r.step, r.index)))
    return record.model_copy(update={"runs": runs})


def with_followup(record: GroupRecord, followup: FollowUp) -> GroupRecord:
    """替换或追加一次追问。"""
    key = (followup.asked_by, followup.target)
    others = tuple(f for f in record.followups if (f.asked_by, f.target) != key)
    ordered = tuple(sorted((*others, followup), key=lambda f: (f.asked_by, f.target)))
    return record.model_copy(update={"followups": ordered})


def with_verdict(record: GroupRecord, verdict: StepVerdict) -> GroupRecord:
    """替换或追加一步的裁决。"""
    others = tuple(v for v in record.verdicts if v.step != verdict.step)
    return record.model_copy(update={"verdicts": tuple(sorted((*others, verdict),
                                                              key=lambda v: v.step))})


def step_outcome(record: GroupRecord, step: int) -> StepOutcome | None:
    """一步是否结束、结果如何；还有成员未出结果时返回 None。

    并行与会诊：全部成员出结果才结束，全部完成算完成，否则算失败（部分失败如实保留在成员里）。
    接力：任一成员失败即结束且算失败；全部依次完成算完成。
    """
    members = record.plan.steps[step].members
    runs = [record.run(step, i) for i in range(len(members))]
    outcomes = [None if r is None else r.outcome for r in runs]
    if record.plan.steps[step].mode == "relay":
        for outcome in outcomes:
            if outcome is None:
                return None
            if outcome != "completed":
                return "cancelled" if outcome == "cancelled" else "failed"
        return "completed"
    if any(o is None for o in outcomes):
        return None
    if any(o == "cancelled" for o in outcomes):
        return "cancelled"
    return "completed" if all(o == "completed" for o in outcomes) else "failed"


def next_step(record: GroupRecord, finished: StepOutcome) -> tuple[int | None, tuple[StepOutcome, ...]]:
    """前一步以 ``finished`` 结束后，下一步是哪一步（None 表示计划走完），以及沿途跳过的步。"""
    outcomes: list[StepOutcome] = [*record.step_outcomes, finished]
    index = record.step + 1
    while index < len(record.plan.steps):
        when = record.plan.steps[index].when
        if when is None or when == finished:
            return index, tuple(outcomes)
        outcomes.append("skipped")
        index += 1
    return None, tuple(outcomes)


def final_state(outcomes: tuple[StepOutcome, ...]) -> GroupState:
    """计划走完后的编组终态：取消优先；有失败的步算失败。"""
    if "cancelled" in outcomes:
        return "cancelled"
    return "failed" if "failed" in outcomes else "completed"
