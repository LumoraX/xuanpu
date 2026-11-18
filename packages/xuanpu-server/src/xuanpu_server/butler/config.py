"""管家配置：按租户白名单开启，预算上限只经平台装配注入。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, StrictBool


class ButlerConfig(BaseModel):
    """管家开关与每条 trace / 每个租户的预算上限。

    Attributes:
        enabled: 总开关；与 ``tenants`` 同时满足才开启，空名单始终关闭。
        tenants: 开启的租户。
        max_artisans_per_trace: 一条 trace 最多派出的匠人数（含已结束的）。
        max_butler_turns_per_trace: 一条 trace 里管家最多跑几轮（含被唤醒的轮次）。
        max_tokens_per_trace: 一条 trace 的 token 上限（输入加输出，管家与匠人合计）。
        max_active_traces_per_tenant: 租户同时进行中的派工 trace 上限。
        wake_interval_seconds: 唤醒对账的间隔（看匠人是否结束或提问）。
        max_group_members: 编组每一步最多几个成员（R5-D）。
        max_group_steps: 编组计划最多几步（R5-D）。
        max_consult_rounds: 会诊里 @ 追问最多几轮，超过由管家收口（R5-D2）。
        reply_window_seconds: 员工说一句话后请求最多等多久；没办完先回「进行中」，这一轮在平台里继续（R6-A）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    enabled: StrictBool = False
    tenants: tuple[str, ...] = Field(default=(), max_length=1000)
    max_artisans_per_trace: int = Field(default=4, ge=1, le=16)
    max_butler_turns_per_trace: int = Field(default=8, ge=1, le=64)
    max_tokens_per_trace: int = Field(default=400_000, ge=1_000, le=20_000_000)
    max_active_traces_per_tenant: int = Field(default=50, ge=1, le=10_000)
    wake_interval_seconds: float = Field(default=2.0, ge=0.2, le=300.0)
    max_group_members: int = Field(default=4, ge=1, le=8)
    max_group_steps: int = Field(default=3, ge=1, le=5)
    max_consult_rounds: int = Field(default=2, ge=0, le=5)
    reply_window_seconds: float = Field(default=20.0, ge=1.0, le=120.0)

    def allows(self, tenant: str) -> bool:
        """不能借空名单推导全企业开启。"""
        return self.enabled and tenant in self.tenants
