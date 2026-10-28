"""从 OpenAI 兼容接口读真实模型列表（`GET /v1/models`）。

为什么要有这个模块：模型名会变（`deepseek-v4-flash` 已下线、`gpt-image-2.5`
是新加的），写死在代码里必然过期 —— 而过期**不会报任何错**，只是配置页里
少几个能选的、多几个不存在的。实测本网关：

- 对话密钥能看到 8 个对话模型（deepseek / glm / kimi / minimax / …）
- 生图密钥只能看到 4 个 `gpt-image-*`

所以**必须用对应那把密钥去问对应那一侧**，拿一把 key 拉一张大表会把两边混在
一起，选错了只会在运行时 400。
"""

from __future__ import annotations

from typing import Any

from haiqbot.plugin_api import ProviderError

#: 拉列表是「点一下就要有反馈」的操作，别用生图的 180s 超时
DEFAULT_TIMEOUT_SECONDS = 12.0


async def fetch_models(
    base_url: str,
    headers: dict[str, str],
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> list[str]:
    """返回该接口认得的模型 id（已排序、去重）。

    抛 `ProviderError` 时消息里**只有状态码与地址**，不含任何 header / 密钥。
    """
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise ProviderError("没填 API 基地址，无法拉取模型列表")
    url = f"{base}/models"

    import httpx

    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
    except Exception as exc:  # noqa: BLE001 - 网络异常可能带 URL，只报类型
        raise ProviderError(f"请求 {url} 失败：{type(exc).__name__}") from exc

    if resp.status_code >= 400:
        raise ProviderError(f"{url} 返回 {resp.status_code}")
    try:
        body: Any = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise ProviderError(f"{url} 返回了非 JSON 响应") from exc

    rows = body.get("data") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        raise ProviderError(f"{url} 没有返回 data 数组（不是 OpenAI 兼容接口？）")

    names = {
        str(row.get("id")).strip()
        for row in rows
        if isinstance(row, dict) and str(row.get("id") or "").strip()
    }
    if not names:
        raise ProviderError(f"{url} 返回了空模型列表")
    return sorted(names)
