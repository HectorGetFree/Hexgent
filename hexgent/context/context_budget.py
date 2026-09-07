"""模型活动上下文的容量与压缩配置。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ContextLimits:
    """控制何时压缩上下文以及压缩后保留多少近期内容。"""

    # 当前模型能够接收的最大上下文 Token 数量。
    context_window_tokens: int = 128_000

    # 为主模型正常回答预留的输出 Token 数量。
    reserved_output_tokens: int = 8_000

    # 为 Token 估算误差和请求固定内容预留的安全余量。
    safety_margin_tokens: int = 8_000

    # 即使物理窗口尚有余量，也在此目标规模附近主动压缩，避免长请求显著拖慢模型。
    proactive_threshold_tokens: int = 64_000

    # 单次上下文摘要允许生成的最大 Token 数量。
    summary_output_tokens: int = 2_000

    # 压缩后尽量原样保留的最近消息 Token 数量。
    recent_context_tokens: int = 24_000

    # API 拒绝过长请求后，应急压缩尽量原样保留的最近消息 Token 数量。
    reactive_recent_context_tokens: int = 8_000

    # 同一会话允许连续发生的上下文压缩失败次数。
    max_compaction_attempts: int = 2

    # 一次压缩至少应减少的绝对估算 Token 数量。
    min_compaction_savings_tokens: int = 2_000

    # 一次压缩至少应减少的估算 Token 比例。
    min_compaction_savings_ratio: float = 0.1

    # 连续压缩流程最多允许接受的部分有效压缩次数。
    max_partial_compactions: int = 1

    @property
    def compaction_threshold_tokens(self) -> int:
        """返回触发主动压缩的上下文 Token 阈值。"""
        physical_threshold = max(
            1,
            self.context_window_tokens - self.reserved_output_tokens - self.safety_margin_tokens,
        )
        return min(physical_threshold, self.proactive_threshold_tokens)

    @property
    def blocking_threshold_tokens(self) -> int:
        """返回不压缩便不能安全调用主模型的硬阈值。"""
        return max(1, self.context_window_tokens - self.reserved_output_tokens)
