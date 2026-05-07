from __future__ import annotations

import asyncio
import hashlib
import random
from datetime import timedelta
from typing import Optional
from urllib.parse import urljoin

import feedparser
import httpx
from sqlalchemy.orm import Session

from .database import (
    SeenItem,
    TokenInstance,
    WatchList,
    add_log,
    get_setting,
    session_scope,
    utc_now,
)
from .graphql_repair import GraphqlRepairClient, GraphqlRepairError, TwitterAccount
from .notifier import format_alert, format_feed_item, send_apprise, send_telegram


class Watcher:
    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._stopping = asyncio.Event()
        self._lock = asyncio.Lock()
        self._cursor = 0

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            await self._task

    async def trigger_once(self) -> None:
        async with self._lock:
            await self.run_once()

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                async with self._lock:
                    await self.run_once()
            except Exception as exc:
                with session_scope() as db:
                    add_log(db, "ERROR", f"watcher 主循环异常: {exc}")
            interval = read_int_setting("global_poll_seconds", 5)
            jitter = random.uniform(0.2, 1.5)
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=max(1, interval) + jitter)
            except asyncio.TimeoutError:
                pass

    async def run_once(self) -> None:
        with session_scope() as db:
            pair = self._next_pair(db)
            if not pair:
                return
            token_id, list_id = pair

        await self.poll_pair(token_id, list_id)

    def _next_pair(self, db: Session) -> Optional[tuple[int, int]]:
        now = utc_now()
        pairs = (
            db.query(TokenInstance.id, WatchList.id)
            .join(WatchList, WatchList.token_id == TokenInstance.id)
            .filter(TokenInstance.enabled.is_(True), WatchList.enabled.is_(True))
            .filter((TokenInstance.cooldown_until.is_(None)) | (TokenInstance.cooldown_until <= now))
            .order_by(TokenInstance.id.asc(), WatchList.id.asc())
            .all()
        )
        if not pairs:
            return None
        self._cursor = self._cursor % len(pairs)
        token_id, list_row_id = pairs[self._cursor]
        self._cursor += 1
        return int(token_id), int(list_row_id)

    async def poll_pair(self, token_id: int, list_row_id: int) -> None:
        with session_scope() as db:
            token = db.query(TokenInstance).filter(TokenInstance.id == token_id).first()
            watch_list = db.query(WatchList).filter(WatchList.id == list_row_id).first()
            if not token or not watch_list:
                return
            token_snapshot = snapshot_token(token)
            list_snapshot = snapshot_list(watch_list)
            should_check_subscription = watch_list.subscription_checked_at is None
            bootstrap = (
                db.query(SeenItem)
                .filter(SeenItem.token_id == token.id, SeenItem.list_id == watch_list.list_id)
                .first()
                is None
            )

        try:
            if should_check_subscription:
                await ensure_list_subscription(token_snapshot, list_snapshot)
            if token_snapshot["use_fallback"]:
                items = await fetch_fallback_items(token_snapshot, list_snapshot)
            else:
                items = await fetch_rss_items(token_snapshot, list_snapshot)
            await self.process_items(token_snapshot, list_snapshot, items, bootstrap)
            with session_scope() as db:
                token = db.query(TokenInstance).filter(TokenInstance.id == token_id).first()
                if token:
                    token.healthy = True
                    token.last_error = ""
                    token.last_checked_at = utc_now()
                    token.updated_at = utc_now()
                add_log(db, "INFO", f"{token_snapshot['name']} / {list_snapshot['list_id']} 检查完成，返回 {len(items)} 条")
        except Exception as exc:
            await self.handle_source_failure(token_snapshot, list_snapshot, str(exc))

    async def process_items(
        self,
        token: dict,
        watch_list: dict,
        items: list[dict],
        bootstrap: bool,
    ) -> None:
        bot_token, chat_id, apprise_urls = read_notify_settings()
        for item in reversed(items):
            item_id = item.get("id") or stable_id(item.get("link", ""), item.get("title", ""))
            title = item.get("title", "")
            link = item.get("link", "")
            with session_scope() as db:
                exists = db.query(SeenItem).filter(SeenItem.item_id == item_id).first()
                if exists:
                    continue
                seen = SeenItem(
                    item_id=item_id,
                    list_id=watch_list["list_id"],
                    token_id=token["id"],
                    title=title,
                    link=link,
                    forwarded_at=None if bootstrap else utc_now(),
                )
                db.add(seen)
                if bootstrap:
                    add_log(db, "INFO", f"首次启动记录历史 item: {item_id}")
                    continue
                add_log(db, "INFO", f"发现新 item: {item_id}")

            message = format_feed_item(title, link, watch_list["name"] or watch_list["list_id"])
            try:
                await send_telegram(bot_token, chat_id, message)
                if apprise_urls:
                    send_apprise(apprise_urls, "X List 更新", f"{title}\n{link}")
            except Exception as exc:
                with session_scope() as db:
                    add_log(db, "ERROR", f"推送 item 失败 {item_id}: {exc}")

    async def handle_source_failure(self, token: dict, watch_list: dict, error: str) -> None:
        bot_token, chat_id, _ = read_notify_settings()
        title = "X/RSSHub 抓取异常"
        body = f"{token['name']} / List {watch_list['list_id']} 抓取失败，准备自动发现 GraphQL ID 并尝试 fallback 修复。"
        with session_scope() as db:
            row = db.query(TokenInstance).filter(TokenInstance.id == token["id"]).first()
            if row:
                row.healthy = False
                row.last_error = error[:2000]
                row.last_checked_at = utc_now()
                row.updated_at = utc_now()
            add_log(db, "ERROR", f"{body} 原因: {error}")
        await notify_safely(bot_token, chat_id, format_alert(title, body, error))

        success, detail, query_id = await attempt_graphql_repair(token, watch_list)
        with session_scope() as db:
            row = db.query(TokenInstance).filter(TokenInstance.id == token["id"]).first()
            if row:
                row.last_repaired_at = utc_now()
                row.updated_at = utc_now()
                if success:
                    row.use_fallback = True
                    row.graphql_query_id = query_id
                    row.healthy = True
                    row.last_error = ""
                    row.cooldown_until = None
                else:
                    row.healthy = False
                    row.last_error = detail[:2000]
                    row.cooldown_until = utc_now() + timedelta(minutes=read_int_setting("failure_cooldown_minutes", 10))
            add_log(db, "INFO" if success else "ERROR", detail)

        if success:
            await notify_safely(
                bot_token,
                chat_id,
                format_alert("GraphQL ID 自动修复成功", f"{token['name']} 已切换到 fallback 抓取。", f"query_id={query_id}\n{detail}"),
            )
        else:
            await notify_safely(
                bot_token,
                chat_id,
                format_alert("GraphQL ID 自动修复失败", f"{token['name']} 已进入冷却，等待人工处理或 RSSHub 更新。", detail),
            )


async def fetch_rss_items(token: dict, watch_list: dict) -> list[dict]:
    base = token["rsshub_url"].rstrip("/") + "/"
    url = urljoin(base, f"twitter/list/{watch_list['list_id']}")
    async with httpx.AsyncClient(timeout=35.0) as client:
        resp = await client.get(url)
    if resp.status_code >= 400:
        raise RuntimeError(f"RSSHub HTTP {resp.status_code}: {resp.text[:300]}")
    parsed = feedparser.parse(resp.text)
    if parsed.bozo:
        raise RuntimeError(f"RSS 解析失败: {parsed.bozo_exception}")
    entries = []
    for entry in parsed.entries:
        entries.append(
            {
                "id": entry.get("id") or entry.get("guid") or entry.get("link"),
                "title": entry.get("title", ""),
                "link": entry.get("link", ""),
            }
        )
    return entries


async def fetch_fallback_items(token: dict, watch_list: dict) -> list[dict]:
    if not token["graphql_query_id"]:
        raise RuntimeError("fallback 已启用但没有 graphql query id")
    client = GraphqlRepairClient(
        TwitterAccount(
            auth_token=token["auth_token"],
            ct0=token["ct0"],
            bearer_token=token["bearer_token"],
            proxy_url=token["proxy_url"],
        )
    )
    try:
        return await client.fetch_list_tweets(watch_list["list_id"], token["graphql_query_id"], count=10)
    finally:
        await client.close()


async def ensure_list_subscription(token: dict, watch_list: dict) -> None:
    if not token["auth_token"] or not token["ct0"]:
        detail = "未填写 auth_token 或 ct0，跳过自动关注 List。"
        with session_scope() as db:
            row = db.query(WatchList).filter(WatchList.id == watch_list["id"]).first()
            if row:
                row.subscription_checked_at = utc_now()
                row.subscription_error = detail
            add_log(db, "WARNING", f"{token['name']} / {watch_list['list_id']}: {detail}")
        return

    client = GraphqlRepairClient(
        TwitterAccount(
            auth_token=token["auth_token"],
            ct0=token["ct0"],
            bearer_token=token["bearer_token"],
            proxy_url=token["proxy_url"],
        )
    )
    try:
        query_id = await client.subscribe_list(watch_list["list_id"])
        with session_scope() as db:
            row = db.query(WatchList).filter(WatchList.id == watch_list["id"]).first()
            if row:
                row.subscription_checked_at = utc_now()
                row.subscription_error = ""
            add_log(db, "INFO", f"{token['name']} / {watch_list['list_id']} 已确认或自动关注 List，query_id={query_id}")
    except (GraphqlRepairError, httpx.HTTPError, RuntimeError) as exc:
        detail = str(exc)[:1000]
        with session_scope() as db:
            row = db.query(WatchList).filter(WatchList.id == watch_list["id"]).first()
            if row:
                row.subscription_checked_at = utc_now()
                row.subscription_error = detail
            add_log(db, "WARNING", f"{token['name']} / {watch_list['list_id']} 自动关注 List 失败，将继续尝试抓取: {detail}")
    finally:
        await client.close()


async def attempt_graphql_repair(token: dict, watch_list: dict) -> tuple[bool, str, str]:
    client = GraphqlRepairClient(
        TwitterAccount(
            auth_token=token["auth_token"],
            ct0=token["ct0"],
            bearer_token=token["bearer_token"],
            proxy_url=token["proxy_url"],
        )
    )
    try:
        query_id = await client.discover_query_id("ListLatestTweetsTimeline")
        tweets = await client.fetch_list_tweets(watch_list["list_id"], query_id, count=3)
        return True, f"发现 query id {query_id}，fallback 测试返回 {len(tweets)} 条。", query_id
    except (GraphqlRepairError, httpx.HTTPError, RuntimeError) as exc:
        return False, str(exc), ""
    finally:
        await client.close()


async def notify_safely(bot_token: str, chat_id: str, message: str) -> None:
    try:
        await send_telegram(bot_token, chat_id, message)
    except Exception as exc:
        with session_scope() as db:
            add_log(db, "ERROR", f"TG 报警发送失败: {exc}")


def read_notify_settings() -> tuple[str, str, str]:
    with session_scope() as db:
        return (
            get_setting(db, "telegram_bot_token", ""),
            get_setting(db, "telegram_chat_id", ""),
            get_setting(db, "apprise_urls", ""),
        )


def read_int_setting(key: str, default: int) -> int:
    with session_scope() as db:
        value = get_setting(db, key, str(default))
    try:
        return int(value)
    except ValueError:
        return default


def snapshot_token(token: TokenInstance) -> dict:
    return {
        "id": token.id,
        "name": token.name,
        "rsshub_url": token.rsshub_url,
        "auth_token": token.auth_token,
        "ct0": token.ct0,
        "bearer_token": token.bearer_token,
        "proxy_url": token.proxy_url,
        "use_fallback": token.use_fallback,
        "graphql_query_id": token.graphql_query_id,
    }


def snapshot_list(watch_list: WatchList) -> dict:
    return {
        "id": watch_list.id,
        "name": watch_list.name,
        "list_id": watch_list.list_id,
    }


def stable_id(link: str, title: str) -> str:
    digest = hashlib.sha256(f"{link}\n{title}".encode("utf-8")).hexdigest()
    return f"feed:{digest}"


watcher = Watcher()
