from __future__ import annotations

import html
import re
from typing import Optional

import apprise
import httpx


class NotifyError(RuntimeError):
    pass


def html_to_text(value: str) -> str:
    if not value:
        return ""
    text = html.unescape(value)
    text = re.sub(r"(?i)<\s*br\s*/?\s*>", "\n", text)
    text = re.sub(r"(?i)</\s*(p|div|li|tr)\s*>", "\n", text)
    text = re.sub(r"(?i)<\s*(p|div|li|tr|hr)\b[^>]*>", "\n", text)
    text = re.sub(r"(?i)<\s*/?\s*video\b[^>]*>", "\n", text)
    text = re.sub(r"(?i)<[^>]+>", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def clip_text(value: str, limit: int = 2800) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def extract_media_urls(value: str) -> list[str]:
    if not value:
        return []
    urls: list[str] = []
    for match in re.finditer(r"""(?i)\b(?:src|href|poster)=["']([^"']+)["']""", value):
        url = html.unescape(match.group(1)).strip()
        if url and url not in urls:
            urls.append(url)
    return urls


async def send_telegram(
    bot_token: str,
    chat_id: str,
    text: str,
    button_text: str = "",
    button_url: str = "",
) -> None:
    if not bot_token or not chat_id:
        raise NotifyError("Telegram bot token 或 chat id 未配置")
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if button_text and button_url:
        payload["reply_markup"] = {
            "inline_keyboard": [[{"text": button_text, "url": button_url}]]
        }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            url,
            json=payload,
        )
    if resp.status_code >= 400:
        raise NotifyError(f"Telegram 推送失败: {resp.status_code} {resp.text[:300]}")


def send_apprise(urls: str, title: str, body: str) -> bool:
    targets = [line.strip() for line in urls.replace(",", "\n").splitlines() if line.strip()]
    if not targets:
        return True
    app = apprise.Apprise()
    for target in targets:
        app.add(target)
    return bool(app.notify(title=title, body=body))


def format_alert(title: str, body: str, detail: Optional[str] = None) -> str:
    message = f"<b>{html.escape(title)}</b>\n{html.escape(body)}"
    if detail:
        message += f"\n\n<code>{html.escape(detail[:1200])}</code>"
    return message


def format_feed_item(
    title: str,
    source: str = "",
    author_label: str = "",
    body_text: str = "",
    translated_title: str = "",
) -> str:
    parts = []
    if author_label:
        parts.append(html.escape(author_label))
    if title:
        parts.append(html.escape(title))
    cleaned_body = html_to_text(body_text)
    if cleaned_body:
        parts.append(html.escape(clip_text(cleaned_body)))
    media_urls = extract_media_urls(body_text)
    if media_urls:
        if parts:
            parts.append("")
        parts.append("<b>媒体链接</b>")
        for url in media_urls[:3]:
            parts.append(html.escape(url))
    body = "\n".join(parts)
    if translated_title:
        if body:
            body += "\n\n"
        body += f"<b>中文翻译</b>\n{html.escape(translated_title)}"
    return body
