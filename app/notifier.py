from __future__ import annotations

import html
from typing import Optional

import apprise
import httpx


class NotifyError(RuntimeError):
    pass


async def send_telegram(bot_token: str, chat_id: str, text: str) -> None:
    if not bot_token or not chat_id:
        raise NotifyError("Telegram bot token 或 chat id 未配置")
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            url,
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
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
    link: str,
    source: str = "",
    author_label: str = "",
    translated_title: str = "",
) -> str:
    heading = "X List 更新"
    if source:
        heading += f" - {source}"
    safe_link = html.escape(link)
    author_line = f"{html.escape(author_label)}\n" if author_label else ""
    body = (
        f"<b>{html.escape(heading)}</b>\n"
        f"{author_line}"
        f"{html.escape(title)}"
    )
    if translated_title:
        body += f"\n\n<b>中文翻译</b>\n{html.escape(translated_title)}"
    body += f"\n\n<a href=\"{safe_link}\">打开原文</a>"
    return body
