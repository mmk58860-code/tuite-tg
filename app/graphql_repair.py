from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Iterable, Optional
from urllib.parse import quote

import httpx


WEB_BEARER_FALLBACK = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAA"
    "N0Xq8e6V9m4uPU0KX03q6Rtw0w8%3D"
    "VnUdz8RbjxU8G7cBU7vKhSx0JY0I9M1nJbUPgA"
)

FEATURES = {
    "rweb_tipjar_consumption_enabled": True,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "verified_phone_label_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "communities_web_enable_tweet_community_results_fetch": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "articles_preview_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "tweet_awards_web_tipping_enabled": False,
    "creator_subscriptions_quote_tweet_preview_enabled": False,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "rweb_video_timestamps_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
}


@dataclass
class TwitterAccount:
    auth_token: str
    ct0: str = ""
    bearer_token: str = ""
    proxy_url: str = ""


class GraphqlRepairError(RuntimeError):
    pass


class GraphqlRepairClient:
    def __init__(self, account: TwitterAccount) -> None:
        self.account = account
        transport = httpx.AsyncHTTPTransport(proxy=account.proxy_url) if account.proxy_url else None
        self.client = httpx.AsyncClient(
            transport=transport,
            timeout=35.0,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                ),
                "Accept": "*/*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def discover_query_id(self, operation: str = "ListLatestTweetsTimeline") -> str:
        home = await self.client.get("https://x.com/home")
        text = home.text.replace("\\/", "/")
        scripts = re.findall(
            r'https://abs\.twimg\.com/responsive-web/client-web/[^"]+\.js',
            text,
        )
        for script_url in dict.fromkeys(scripts):
            try:
                script = await self.client.get(script_url)
            except httpx.HTTPError:
                continue
            query_id = find_graphql_query_id(script.text, operation)
            if query_id:
                return query_id
        raise GraphqlRepairError(f"无法自动发现 {operation} 的 GraphQL query id")

    async def discover_web_bearer(self) -> str:
        resp = await self.client.get("https://x.com/")
        if resp.status_code < 400:
            match = re.search(r"Bearer ([A-Za-z0-9%._-]+)", resp.text)
            if match:
                return match.group(1)
        return WEB_BEARER_FALLBACK

    async def fetch_list_tweets(self, list_id: str, query_id: str, count: int = 5) -> list[dict]:
        if not self.account.auth_token:
            raise GraphqlRepairError("fallback 抓取需要 auth_token")
        if not self.account.ct0:
            raise GraphqlRepairError("fallback 抓取需要 ct0")

        bearer = self.account.bearer_token or await self.discover_web_bearer()
        variables = {"listId": str(list_id), "count": min(max(count, 1), 20)}
        headers = {
            "Authorization": f"Bearer {bearer}",
            "Cookie": f"auth_token={self.account.auth_token}; ct0={self.account.ct0}",
            "X-Csrf-Token": self.account.ct0,
            "X-Twitter-Active-User": "yes",
            "X-Twitter-Auth-Type": "OAuth2Session",
            "Referer": f"https://x.com/i/lists/{list_id}",
        }
        resp = await self.client.get(
            f"https://x.com/i/api/graphql/{query_id}/ListLatestTweetsTimeline",
            headers=headers,
            params={
                "variables": json.dumps(variables, separators=(",", ":")),
                "features": json.dumps(FEATURES, separators=(",", ":")),
            },
        )
        if resp.status_code >= 400:
            raise GraphqlRepairError(f"fallback 请求失败: {resp.status_code} {resp.text[:300]}")
        return extract_tweets(resp.json())

    async def subscribe_list(self, list_id: str) -> str:
        if not self.account.auth_token:
            raise GraphqlRepairError("自动关注 List 需要 auth_token")
        if not self.account.ct0:
            raise GraphqlRepairError("自动关注 List 需要 ct0")

        query_id = await self.discover_query_id("ListSubscribe")
        bearer = self.account.bearer_token or await self.discover_web_bearer()
        headers = {
            "Authorization": f"Bearer {bearer}",
            "Cookie": f"auth_token={self.account.auth_token}; ct0={self.account.ct0}",
            "X-Csrf-Token": self.account.ct0,
            "X-Twitter-Active-User": "yes",
            "X-Twitter-Auth-Type": "OAuth2Session",
            "Referer": f"https://x.com/i/lists/{list_id}",
        }
        payload = {
            "variables": {"listId": str(list_id)},
            "features": {
                "responsive_web_graphql_exclude_directive_enabled": True,
                "verified_phone_label_enabled": False,
                "responsive_web_graphql_timeline_navigation_enabled": True,
            },
        }
        resp = await self.client.post(
            f"https://x.com/i/api/graphql/{query_id}/ListSubscribe",
            headers=headers,
            json=payload,
        )
        if resp.status_code >= 400:
            raise GraphqlRepairError(f"自动关注 List 失败: {resp.status_code} {resp.text[:300]}")
        body = resp.text[:300]
        if '"errors"' in body:
            raise GraphqlRepairError(f"自动关注 List 返回错误: {body}")
        return query_id


def find_graphql_query_id(source: str, operation: str) -> str:
    patterns = [
        rf'queryId:"([A-Za-z0-9_-]+)",operationName:"{re.escape(operation)}"',
        rf'operationName:"{re.escape(operation)}",queryId:"([A-Za-z0-9_-]+)"',
        rf'queryId:\s*"([A-Za-z0-9_-]+)"[^}}]{{0,300}}{re.escape(operation)}',
    ]
    for pattern in patterns:
        match = re.search(pattern, source)
        if match:
            return match.group(1)
    return ""


def extract_tweets(payload: dict) -> list[dict]:
    tweets = []
    seen = set()
    for node in walk(payload):
        result = None
        if isinstance(node, dict):
            if node.get("__typename") == "Tweet":
                result = node
            elif node.get("tweet_results"):
                result = node.get("tweet_results", {}).get("result")
            elif node.get("tweet"):
                result = node.get("tweet")
        if not isinstance(result, dict):
            continue
        normalized = normalize_web_tweet(result)
        if normalized and normalized["id"] not in seen:
            seen.add(normalized["id"])
            tweets.append(normalized)
    return tweets


def walk(value) -> Iterable:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def normalize_web_tweet(item: dict) -> Optional[dict]:
    legacy = item.get("legacy") or {}
    if item.get("__typename") == "TweetWithVisibilityResults":
        item = item.get("tweet") or item
        legacy = item.get("legacy") or {}
    tweet_id = item.get("rest_id") or legacy.get("id_str")
    if not tweet_id or legacy.get("retweeted_status_result"):
        return None

    user_result = item.get("core", {}).get("user_results", {}).get("result", {})
    user_legacy = user_result.get("legacy") or {}
    username = user_legacy.get("screen_name") or ""
    name = user_legacy.get("name") or username
    text = (
        legacy.get("full_text")
        or legacy.get("text")
        or item.get("note_tweet", {}).get("note_tweet_results", {}).get("result", {}).get("text")
        or ""
    )
    return {
        "id": str(tweet_id),
        "title": f"{name} (@{username}): {text[:160]}",
        "link": f"https://x.com/{quote(username)}/status/{tweet_id}" if username else f"https://x.com/i/web/status/{tweet_id}",
        "username": username,
        "text": text,
    }
