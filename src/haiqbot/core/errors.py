"""异常体系。"""

from __future__ import annotations


class HaiqError(Exception):
    """haiqbot 所有异常的基类。"""


class StopPipeline(HaiqError):
    """中间件短路：终止后续环节，不再分发到插件。"""

    def __init__(self, reason: str, *, silent: bool = True) -> None:

        super().__init__(reason)

        self.reason = reason

        self.silent = silent


class CapabilityDenied(HaiqError):
    """插件调用了未在 manifest 中声明的能力。"""


class CapabilityUnavailable(HaiqError):
    """能力已声明，但当前没有插件提供实现。



    典型场景：把 image-service 插件停用后，painter 还在跑。

    消费方应当降级提示，而不是把栈抛给用户。

    """


class HandlerTimeout(HaiqError):
    """插件处理超时。"""


class AdapterError(HaiqError):
    """适配层错误。"""


class MediaSendError(AdapterError):
    """媒体消息（图片/语音/视频/文件）没能送达。

    与普通 AdapterError 的区别：
    - **不重试**：重试只会加重平台风控，还可能造成重复消息；
    - 调用方（插件）应该把原因说清楚，让群里知道"图生成成功了，但发送失败了"。
    """


class RateLimited(AdapterError):
    """平台明确回报「发送过于频繁，本次没发出去」。

    为什么单独一个类型
    ------------------
    出站闸门的重试判据必须知道「能不能安全重试」，而这件事**只有适配器知道**：
    平台错误码（OneBot 的 `retcode`、官方开放平台的 `code`）是实现细节，
    内核不该认识任何一个。

    所以约定：适配器把平台错误码翻成语义化异常，内核只认类型——

    - `RateLimited`  → 明确没发出去，退避后**可以重试**
    - `MediaSendError` → 媒体通道失败，重试只会加重风控，**不重试**
    - 其余 `AdapterError` → 情况不明（可能已送达），**不重试**
    """


class ContractError(HaiqError):
    """插件之间的服务契约不满足：声明了依赖但没人提供、版本不够、或实现缺方法。"""


class VaultMissing(HaiqError):
    """缺少必要凭据。"""


class ConfigError(HaiqError):
    """配置文件读不出来：编码不对、TOML 语法坏、字段放错位置等。

    CLI 捕获后只打印一条人话，不甩 traceback —— Windows 上「另存为带 BOM 的
    UTF-8」是最常见的一类，用户看不懂 `Invalid statement`。
    """
