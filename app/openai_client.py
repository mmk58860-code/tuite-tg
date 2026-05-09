from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx


class OpenAIConfigError(RuntimeError):
    pass


class OpenAIRequestError(RuntimeError):
    pass


@dataclass
class OpenAIEndpoint:
    api_key: str
    model: str
    base_url: str
    organization_id: str = ""
    project_id: str = ""


def build_endpoint(
    api_key: str,
    model: str,
    base_url: str,
    organization_id: str = "",
    project_id: str = "",
) -> OpenAIEndpoint:
    clean_key = api_key.strip()
    clean_model = model.strip()
    clean_base = (base_url or "https://api.openai.com/v1").strip().rstrip("/")
    if not clean_key:
        raise OpenAIConfigError("OpenAI API Key 未填写")
    if not clean_model:
        raise OpenAIConfigError("OpenAI 模型未填写")
    return OpenAIEndpoint(
        api_key=clean_key,
        model=clean_model,
        base_url=clean_base,
        organization_id=organization_id.strip(),
        project_id=project_id.strip(),
    )


def _headers(endpoint: OpenAIEndpoint) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {endpoint.api_key}",
        "Content-Type": "application/json",
    }
    if endpoint.organization_id:
        headers["OpenAI-Organization"] = endpoint.organization_id
    if endpoint.project_id:
        headers["OpenAI-Project"] = endpoint.project_id
    return headers


async def translate_text(endpoint: OpenAIEndpoint, text: str) -> str:
    prompt = (
        "请把下面的推文内容翻译成简体中文。"
        "只输出翻译结果，不要解释，不要加引号。"
        "如果原文已经是中文，就直接返回原文。\n\n"
        f"{text.strip()}"
    )
    payload = {
        "model": endpoint.model,
        "input": prompt,
    }
    async with httpx.AsyncClient(timeout=45.0) as client:
        resp = await client.post(
            f"{endpoint.base_url}/responses",
            headers=_headers(endpoint),
            json=payload,
        )
    if resp.status_code >= 400:
        raise OpenAIRequestError(f"翻译请求失败: {resp.status_code} {resp.text[:300]}")
    body = resp.json()
    output_text = str(body.get("output_text") or "").strip()
    if output_text:
        return output_text
    raise OpenAIRequestError("翻译请求成功，但没有返回文本结果")


async def query_recent_costs(endpoint: OpenAIEndpoint, days: int = 30) -> str:
    start_time = int((datetime.now(timezone.utc) - timedelta(days=max(1, days))).timestamp())
    async with httpx.AsyncClient(timeout=45.0) as client:
        resp = await client.get(
            f"{endpoint.base_url}/organization/usage/costs",
            headers=_headers(endpoint),
            params={"start_time": start_time},
        )
    if resp.status_code >= 400:
        raise OpenAIRequestError(f"费用查询失败: {resp.status_code} {resp.text[:300]}")
    body = resp.json()
    total = 0.0
    currency = "USD"
    for row in body.get("data", []):
        amount = row.get("amount") or {}
        value = amount.get("value")
        if value is None:
            continue
        total += float(value)
        currency = str(amount.get("currency") or currency).upper()
    return f"OpenAI 官方未提供通用余额接口，已返回最近 {days} 天费用：{total:.4f} {currency}"
