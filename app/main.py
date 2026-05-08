from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager
from urllib.parse import urljoin

import feedparser
import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from . import auth
from .database import (
    Log,
    ProxyProfile,
    RsshubInstance,
    SeenItem,
    Setting,
    TokenListState,
    TokenInstance,
    UserAlias,
    WatchList,
    add_log,
    get_db,
    get_setting,
    init_db,
    session_scope,
    set_setting,
    utc_now,
)
from .docker_manager import (
    DockerManagerError,
    container_logs,
    create_rsshub_container,
    docker_available,
    inspect_container,
    list_rsshub_containers,
    recreate_rsshub_container,
    remove_container,
)
from .notifier import format_alert, send_telegram
from .watcher import attempt_graphql_repair, snapshot_list, snapshot_token, watcher


load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
BEIJING_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")

TOKEN_EXPORT_FIELDS = [
    "id",
    "name",
    "rsshub_url",
    "auth_token",
    "ct0",
    "bearer_token",
    "proxy_url",
    "enabled",
    "healthy",
    "use_fallback",
    "graphql_query_id",
    "last_error",
    "last_checked_at",
    "last_success_at",
    "last_alerted_at",
    "last_repaired_at",
    "cooldown_until",
    "created_at",
    "updated_at",
]
LIST_EXPORT_FIELDS = [
    "id",
    "token_id",
    "name",
    "list_id",
    "enabled",
    "subscription_checked_at",
    "subscription_error",
    "created_at",
]
SEEN_EXPORT_FIELDS = [
    "item_id",
    "list_id",
    "token_id",
    "title",
    "link",
    "created_at",
    "forwarded_at",
]
USER_ALIAS_EXPORT_FIELDS = [
    "id",
    "username",
    "note",
    "created_at",
    "updated_at",
]
RSSHUB_EXPORT_FIELDS = [
    "id",
    "name",
    "host_port",
    "internal_url",
    "twitter_auth_token",
    "third_party_api",
    "proxy_uri",
    "container_id",
    "status",
    "last_test_at",
    "last_test_ok",
    "last_test_message",
    "created_at",
    "updated_at",
]
PROXY_EXPORT_FIELDS = [
    "id",
    "name",
    "proxy_url",
    "enabled",
    "last_test_at",
    "last_test_ok",
    "last_test_message",
    "created_at",
    "updated_at",
]
PROTECTED_IMPORT_SETTINGS = {"admin_username", "admin_password_hash"}
templates.env.filters["beijing_time"] = lambda value: format_beijing_time(value)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    ensure_defaults()
    watcher.start()
    yield
    await watcher.stop()


app = FastAPI(title="Tuite TG", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


def ensure_defaults() -> None:
    with session_scope() as db:
        defaults = {
            "admin_username": os.getenv("WEB_USERNAME", "admin"),
            "admin_password_hash": auth.get_password_hash(os.getenv("WEB_PASSWORD", "admin12345")),
            "telegram_bot_token": os.getenv("TELEGRAM_BOT_TOKEN", ""),
            "telegram_chat_id": os.getenv("TELEGRAM_CHAT_ID", ""),
            "apprise_urls": os.getenv("APPRISE_URLS", ""),
            "global_poll_seconds": os.getenv("GLOBAL_POLL_SECONDS", "5"),
            "failure_cooldown_minutes": os.getenv("FAILURE_COOLDOWN_MINUTES", "10"),
        }
        for key, value in defaults.items():
            if not get_setting(db, key, ""):
                set_setting(db, key, value)
        migrate_existing_proxies(db)
        add_log(db, "INFO", "Tuite TG 已启动")


def wants_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "")


def token_from_cookie(request: Request) -> str:
    return request.cookies.get("access_token", "").replace("Bearer ", "")


async def current_user_from_cookie(request: Request, db: Session = Depends(get_db)) -> str:
    token = token_from_cookie(request)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return await auth.get_current_user(token=token, db=db)


@app.post("/api/token")
async def login_for_access_token(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
):
    username = get_setting(db, "admin_username", "admin")
    password_hash = get_setting(db, "admin_password_hash", "")
    if form_data.username != username or not auth.verify_password(form_data.password, password_hash):
        raise HTTPException(status_code=401, detail="Incorrect username or password")
    access_token = auth.create_access_token(
        data={"sub": username},
        expires_delta=timedelta(minutes=auth.ACCESS_TOKEN_EXPIRE_MINUTES),
    )
    return {"access_token": access_token, "token_type": "bearer"}


@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    db: Session = Depends(get_db),
):
    try:
        await current_user_from_cookie(request, db)
    except HTTPException:
        return RedirectResponse("/login", status_code=303)
    tokens = db.query(TokenInstance).order_by(TokenInstance.id.asc()).all()
    lists = db.query(WatchList).filter(WatchList.token_id == 0).order_by(WatchList.id.asc()).all()
    rsshub_instances = db.query(RsshubInstance).order_by(RsshubInstance.host_port.asc()).all()
    proxies = db.query(ProxyProfile).order_by(ProxyProfile.id.asc()).all()
    aliases = db.query(UserAlias).order_by(UserAlias.username.asc()).all()
    logs = db.query(Log).order_by(Log.id.desc()).limit(120).all()
    stability_logs = (
        db.query(Log)
        .filter(Log.created_at >= utc_now() - timedelta(hours=24))
        .order_by(Log.created_at.asc())
        .all()
    )
    settings = {
        "telegram_bot_token": get_setting(db, "telegram_bot_token", ""),
        "telegram_chat_id": get_setting(db, "telegram_chat_id", ""),
        "apprise_urls": get_setting(db, "apprise_urls", ""),
        "global_poll_seconds": get_setting(db, "global_poll_seconds", "5"),
        "failure_cooldown_minutes": get_setting(db, "failure_cooldown_minutes", "10"),
    }
    last_checked = max((token.last_checked_at for token in tokens if token.last_checked_at), default=None)
    stats = {
        "total_tokens": len(tokens),
        "enabled_tokens": sum(1 for token in tokens if token.enabled),
        "healthy_tokens": sum(1 for token in tokens if token.healthy and token.enabled and token.last_success_at),
        "unchecked_tokens": sum(1 for token in tokens if token.enabled and not token.last_checked_at and not token.last_success_at and not token.last_error),
        "total_lists": len(lists),
        "enabled_lists": sum(1 for item in lists if item.enabled),
        "total_rsshub": len(rsshub_instances),
        "last_checked_at": last_checked,
        "telegram_ready": bool(settings["telegram_bot_token"] and settings["telegram_chat_id"]),
    }
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "tokens": tokens,
            "lists": lists,
            "rsshub_instances": rsshub_instances,
            "proxies": proxies,
            "docker_available": docker_available(),
            "logs": logs,
            "settings": settings,
            "stats": stats,
            "stability": build_stability_chart(stability_logs),
            "aliases": aliases,
        },
    )


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/login")
async def login_form(
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    expected = get_setting(db, "admin_username", "admin")
    password_hash = get_setting(db, "admin_password_hash", "")
    if username != expected or not auth.verify_password(password, password_hash):
        return RedirectResponse("/login?error=1", status_code=303)
    access_token = auth.create_access_token(
        data={"sub": username},
        expires_delta=timedelta(minutes=auth.ACCESS_TOKEN_EXPIRE_MINUTES),
    )
    response = RedirectResponse("/", status_code=303)
    response.set_cookie("access_token", f"Bearer {access_token}", httponly=True, samesite="lax")
    return response


@app.post("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie("access_token")
    return response


@app.post("/settings")
async def save_settings(
    request: Request,
    telegram_bot_token: str = Form(""),
    telegram_chat_id: str = Form(""),
    apprise_urls: str = Form(""),
    global_poll_seconds: int = Form(5),
    failure_cooldown_minutes: int = Form(10),
    db: Session = Depends(get_db),
    _: str = Depends(current_user_from_cookie),
):
    set_setting(db, "telegram_bot_token", telegram_bot_token.strip())
    set_setting(db, "telegram_chat_id", telegram_chat_id.strip())
    set_setting(db, "apprise_urls", apprise_urls.strip())
    set_setting(db, "global_poll_seconds", str(max(1, global_poll_seconds)))
    set_setting(db, "failure_cooldown_minutes", str(max(1, failure_cooldown_minutes)))
    add_log(db, "INFO", "系统配置已保存")
    return RedirectResponse("/#settings", status_code=303)


@app.post("/settings/test-telegram")
async def test_telegram(
    db: Session = Depends(get_db),
    _: str = Depends(current_user_from_cookie),
):
    bot_token = get_setting(db, "telegram_bot_token", "")
    chat_id = get_setting(db, "telegram_chat_id", "")
    try:
        await send_telegram(bot_token, chat_id, format_alert("Tuite TG", "Telegram 测试消息发送成功。"))
        add_log(db, "INFO", "Telegram 测试消息发送成功")
    except Exception as exc:
        add_log(db, "ERROR", f"Telegram 测试失败: {exc}")
    return RedirectResponse("/#settings", status_code=303)


@app.get("/data/export")
async def export_data(
    db: Session = Depends(get_db),
    _: str = Depends(current_user_from_cookie),
):
    payload = {
        "schema_version": 1,
        "app": "tuite-tg",
        "exported_at": beijing_now().isoformat(),
        "settings": [
            {"key": item.key, "value": item.value}
            for item in db.query(Setting).order_by(Setting.key.asc()).all()
        ],
        "token_instances": [
            serialize_row(item, TOKEN_EXPORT_FIELDS)
            for item in db.query(TokenInstance).order_by(TokenInstance.id.asc()).all()
        ],
        "watch_lists": [
            serialize_row(item, LIST_EXPORT_FIELDS)
            for item in db.query(WatchList).order_by(WatchList.id.asc()).all()
        ],
        "rsshub_instances": [
            serialize_row(item, RSSHUB_EXPORT_FIELDS)
            for item in db.query(RsshubInstance).order_by(RsshubInstance.id.asc()).all()
        ],
        "proxy_profiles": [
            serialize_row(item, PROXY_EXPORT_FIELDS)
            for item in db.query(ProxyProfile).order_by(ProxyProfile.id.asc()).all()
        ],
        "seen_items": [
            serialize_row(item, SEEN_EXPORT_FIELDS)
            for item in db.query(SeenItem).order_by(SeenItem.id.asc()).all()
        ],
        "user_aliases": [
            serialize_row(item, USER_ALIAS_EXPORT_FIELDS)
            for item in db.query(UserAlias).order_by(UserAlias.id.asc()).all()
        ],
    }
    filename = f"tuite-tg-backup-{beijing_now().strftime('%Y%m%d%H%M%S')}.json"
    return Response(
        content=json.dumps(payload, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/data/import")
async def import_data(
    backup_file: UploadFile = File(...),
    confirm_text: str = Form(""),
    db: Session = Depends(get_db),
    _: str = Depends(current_user_from_cookie),
):
    if confirm_text.strip() != "导入":
        add_log(db, "ERROR", "导入取消：确认文字不正确")
        return RedirectResponse("/#settings", status_code=303)

    try:
        raw = await backup_file.read()
        payload = json.loads(raw.decode("utf-8-sig"))
        if payload.get("app") != "tuite-tg":
            raise ValueError("不是 Tuite TG 备份文件")
    except Exception as exc:
        add_log(db, "ERROR", f"导入失败：备份文件无法读取 ({exc})")
        return RedirectResponse("/#settings", status_code=303)

    try:
        db.query(TokenListState).delete()
        db.query(SeenItem).delete()
        db.query(WatchList).delete()
        db.query(RsshubInstance).delete()
        db.query(TokenInstance).delete()
        db.query(ProxyProfile).delete()
        db.query(UserAlias).delete()

        for item in payload.get("settings", []):
            key = str(item.get("key", "")).strip()
            if key and key not in PROTECTED_IMPORT_SETTINGS:
                set_setting(db, key, str(item.get("value", "")))

        for item in payload.get("token_instances", []):
            db.add(TokenInstance(**clean_payload(item, TOKEN_EXPORT_FIELDS)))

        imported_list_ids: set[str] = set()
        for item in payload.get("watch_lists", []):
            clean = clean_payload(item, LIST_EXPORT_FIELDS)
            clean["token_id"] = 0
            list_value = str(clean.get("list_id", "")).strip()
            if not list_value or list_value in imported_list_ids:
                continue
            imported_list_ids.add(list_value)
            clean["list_id"] = list_value
            db.add(WatchList(**clean))

        for item in payload.get("seen_items", []):
            db.add(SeenItem(**clean_payload(item, SEEN_EXPORT_FIELDS)))

        for item in payload.get("rsshub_instances", []):
            clean = clean_payload(item, RSSHUB_EXPORT_FIELDS)
            clean["container_id"] = ""
            clean["status"] = "imported"
            db.add(RsshubInstance(**clean))

        for item in payload.get("proxy_profiles", []):
            clean = clean_payload(item, PROXY_EXPORT_FIELDS)
            if clean.get("proxy_url"):
                db.add(ProxyProfile(**clean))

        for item in payload.get("user_aliases", []):
            db.add(UserAlias(**clean_payload(item, USER_ALIAS_EXPORT_FIELDS)))

        migrate_existing_proxies(db)

        add_log(db, "INFO", "数据导入完成，已覆盖 Token、List、用户备注、系统配置和去重记录")
    except Exception as exc:
        db.rollback()
        add_log(db, "ERROR", f"导入失败：{exc}")
    return RedirectResponse("/#settings", status_code=303)


@app.post("/aliases")
async def save_alias(
    username: str = Form(...),
    note: str = Form(...),
    db: Session = Depends(get_db),
    _: str = Depends(current_user_from_cookie),
):
    clean_username = normalize_username(username)
    clean_note = note.strip()
    if not clean_username or not clean_note:
        add_log(db, "ERROR", "用户备注保存失败：用户名和备注都不能为空")
        return RedirectResponse("/#aliases", status_code=303)
    now = utc_now()
    alias = db.query(UserAlias).filter(UserAlias.username == clean_username).first()
    if alias:
        alias.note = clean_note
        alias.updated_at = now
    else:
        db.add(UserAlias(username=clean_username, note=clean_note, created_at=now, updated_at=now))
    add_log(db, "INFO", f"用户备注已保存: @{clean_username} -> {clean_note}")
    return RedirectResponse("/#aliases", status_code=303)


@app.post("/aliases/{alias_id}/delete")
async def delete_alias(
    alias_id: int,
    db: Session = Depends(get_db),
    _: str = Depends(current_user_from_cookie),
):
    alias = db.query(UserAlias).filter(UserAlias.id == alias_id).first()
    if alias:
        db.delete(alias)
        add_log(db, "INFO", f"用户备注已删除: @{alias.username}")
    return RedirectResponse("/#aliases", status_code=303)


@app.post("/proxies")
async def save_proxy(
    proxy_id: str = Form(""),
    name: str = Form(...),
    proxy_url: str = Form(...),
    enabled: str = Form(""),
    db: Session = Depends(get_db),
    _: str = Depends(current_user_from_cookie),
):
    clean_name = name.strip()
    clean_proxy = proxy_url.strip()
    if not clean_name or not clean_proxy:
        add_log(db, "ERROR", "代理保存失败：名称和代理地址不能为空")
        return RedirectResponse("/#proxies", status_code=303)
    if not is_supported_proxy(clean_proxy):
        add_log(db, "ERROR", "代理保存失败：仅支持 http://、https://、socks5://、socks5h://")
        return RedirectResponse("/#proxies", status_code=303)
    is_enabled = enabled == "on"
    now = utc_now()
    exists = (
        db.query(ProxyProfile)
        .filter((ProxyProfile.name == clean_name) | (ProxyProfile.proxy_url == clean_proxy))
        .first()
    )
    if proxy_id:
        proxy = db.query(ProxyProfile).filter(ProxyProfile.id == int(proxy_id)).first()
        if not proxy:
            raise HTTPException(status_code=404, detail="Proxy not found")
        conflict = (
            db.query(ProxyProfile)
            .filter(ProxyProfile.id != proxy.id)
            .filter((ProxyProfile.name == clean_name) | (ProxyProfile.proxy_url == clean_proxy))
            .first()
        )
        if conflict:
            add_log(db, "ERROR", "代理保存失败：名称或地址已存在")
            return RedirectResponse("/#proxies", status_code=303)
        old_url = proxy.proxy_url
        in_use = (
            db.query(TokenInstance).filter(TokenInstance.proxy_url == old_url).first()
            or db.query(RsshubInstance).filter(RsshubInstance.proxy_uri == old_url).first()
        )
        if in_use and (old_url != clean_proxy or not is_enabled):
            add_log(db, "ERROR", "代理保存失败：该代理正在使用，不能改地址或停用；请先新增并检测新代理，再到 Token/RSSHub 切换。")
            return RedirectResponse("/#proxies", status_code=303)
        proxy.name = clean_name
        proxy.proxy_url = clean_proxy
        proxy.enabled = is_enabled
        proxy.last_test_ok = False if old_url != clean_proxy else proxy.last_test_ok
        proxy.last_test_message = "代理地址已修改，请重新检测。" if old_url != clean_proxy else proxy.last_test_message
        proxy.updated_at = now
    else:
        if exists:
            add_log(db, "ERROR", "代理保存失败：名称或地址已存在")
            return RedirectResponse("/#proxies", status_code=303)
        db.add(
            ProxyProfile(
                name=clean_name,
                proxy_url=clean_proxy,
                enabled=is_enabled,
                last_test_ok=False,
                last_test_message="已保存，尚未检测。",
                created_at=now,
                updated_at=now,
            )
        )
    add_log(db, "INFO", f"代理配置已保存: {clean_name}")
    return RedirectResponse("/#proxies", status_code=303)


@app.post("/proxies/{proxy_id}/test")
async def test_proxy(
    proxy_id: int,
    db: Session = Depends(get_db),
    _: str = Depends(current_user_from_cookie),
):
    proxy = db.query(ProxyProfile).filter(ProxyProfile.id == proxy_id).first()
    if proxy:
        ok, message = await run_proxy_test(proxy.proxy_url)
        proxy.last_test_at = utc_now()
        proxy.last_test_ok = ok
        proxy.last_test_message = message
        proxy.updated_at = utc_now()
        add_log(db, "INFO" if ok else "ERROR", f"代理检测 {proxy.name}: {message}")
    return RedirectResponse("/#proxies", status_code=303)


@app.post("/proxies/test-all")
async def test_all_proxies(
    db: Session = Depends(get_db),
    _: str = Depends(current_user_from_cookie),
):
    proxies = db.query(ProxyProfile).order_by(ProxyProfile.id.asc()).all()
    for proxy in proxies:
        ok, message = await run_proxy_test(proxy.proxy_url)
        proxy.last_test_at = utc_now()
        proxy.last_test_ok = ok
        proxy.last_test_message = message
        proxy.updated_at = utc_now()
        add_log(db, "INFO" if ok else "ERROR", f"批量代理检测 {proxy.name}: {message}")
    return RedirectResponse("/#proxies", status_code=303)


@app.post("/proxies/{proxy_id}/delete")
async def delete_proxy(
    proxy_id: int,
    db: Session = Depends(get_db),
    _: str = Depends(current_user_from_cookie),
):
    proxy = db.query(ProxyProfile).filter(ProxyProfile.id == proxy_id).first()
    if proxy:
        if db.query(TokenInstance).filter(TokenInstance.proxy_url == proxy.proxy_url).first():
            add_log(db, "ERROR", f"代理删除失败：{proxy.name} 正被 Token 使用")
            return RedirectResponse("/#proxies", status_code=303)
        if db.query(RsshubInstance).filter(RsshubInstance.proxy_uri == proxy.proxy_url).first():
            add_log(db, "ERROR", f"代理删除失败：{proxy.name} 正被 RSSHub 使用")
            return RedirectResponse("/#proxies", status_code=303)
        db.delete(proxy)
        add_log(db, "INFO", f"代理已删除: {proxy.name}")
    return RedirectResponse("/#proxies", status_code=303)


@app.post("/tokens")
async def save_token(
    token_id: str = Form(""),
    name: str = Form(...),
    rsshub_url: str = Form(...),
    auth_token: str = Form(""),
    ct0: str = Form(""),
    bearer_token: str = Form(""),
    proxy_url: str = Form(""),
    enabled: str = Form(""),
    db: Session = Depends(get_db),
    _: str = Depends(current_user_from_cookie),
):
    now = utc_now()
    is_enabled = enabled == "on"
    selected_proxy = resolve_proxy_choice(db, proxy_url)
    if proxy_url and selected_proxy is None:
        add_log(db, "ERROR", "Token 保存失败：请选择代理设置里已有且启用的代理")
        return RedirectResponse("/#token-config", status_code=303)
    redirect_url = "/#token-config"
    if token_id:
        token = db.query(TokenInstance).filter(TokenInstance.id == int(token_id)).first()
        if not token:
            raise HTTPException(status_code=404, detail="Token not found")
        redirect_url = token_anchor(token.id)
        old_auth_token = token.auth_token
        old_ct0 = token.ct0
        old_bearer_token = token.bearer_token
        old_proxy_url = token.proxy_url
        token.name = name.strip()
        token.rsshub_url = rsshub_url.strip()
        if auth_token.strip():
            token.auth_token = auth_token.strip()
        if ct0.strip():
            token.ct0 = ct0.strip()
        if bearer_token.strip():
            token.bearer_token = bearer_token.strip()
        token.proxy_url = selected_proxy
        token.enabled = is_enabled
        token.updated_at = now
        if (
            token.auth_token != old_auth_token
            or token.ct0 != old_ct0
            or token.bearer_token != old_bearer_token
            or token.proxy_url != old_proxy_url
        ):
            db.query(TokenListState).filter(TokenListState.token_id == token.id).update(
                {
                    "subscription_checked_at": None,
                    "subscription_error": "",
                    "updated_at": now,
                }
            )
    else:
        token = TokenInstance(
            name=name.strip(),
            rsshub_url=rsshub_url.strip(),
            auth_token=auth_token.strip(),
            ct0=ct0.strip(),
            bearer_token=bearer_token.strip(),
            proxy_url=selected_proxy,
            enabled=is_enabled,
        )
        db.add(token)
    add_log(db, "INFO", f"Token 配置已保存: {name}")
    if not token_id:
        db.flush()
        redirect_url = token_anchor(token.id)
    return RedirectResponse(redirect_url, status_code=303)


@app.post("/tokens/{token_id}/delete")
async def delete_token(
    token_id: int,
    db: Session = Depends(get_db),
    _: str = Depends(current_user_from_cookie),
):
    token = db.query(TokenInstance).filter(TokenInstance.id == token_id).first()
    if token:
        db.query(TokenListState).filter(TokenListState.token_id == token_id).delete()
        db.delete(token)
        add_log(db, "INFO", f"Token 已删除: {token_id}")
    return RedirectResponse("/#token-config", status_code=303)


@app.post("/tokens/{token_id}/check")
async def check_token(
    token_id: int,
    db: Session = Depends(get_db),
    _: str = Depends(current_user_from_cookie),
):
    token = db.query(TokenInstance).filter(TokenInstance.id == token_id).first()
    watch_list = db.query(WatchList).filter(WatchList.token_id == 0, WatchList.enabled.is_(True)).order_by(WatchList.id.asc()).first()
    if not token:
        add_log(db, "ERROR", "手动检测失败：Token 不存在")
        return RedirectResponse("/#token-config", status_code=303)
    if not watch_list:
        token.last_checked_at = utc_now()
        token.healthy = False
        token.last_error = "没有启用的 List，无法检测。"
        token.updated_at = utc_now()
        add_log(db, "ERROR", f"{token.name} 手动检测失败：没有启用的 List")
        return RedirectResponse(token_anchor(token_id), status_code=303)

    await watcher.check_pair(token_id, watch_list.id)
    return RedirectResponse(token_anchor(token_id), status_code=303)


@app.post("/tokens/{token_id}/repair")
async def repair_token(
    token_id: int,
    db: Session = Depends(get_db),
    _: str = Depends(current_user_from_cookie),
):
    token = db.query(TokenInstance).filter(TokenInstance.id == token_id).first()
    watch_list = db.query(WatchList).filter(WatchList.token_id == 0, WatchList.enabled.is_(True)).order_by(WatchList.id.asc()).first()
    if not token or not watch_list:
        add_log(db, "ERROR", "手动修复失败：Token 或启用的 List 不存在")
        return RedirectResponse(token_anchor(token_id), status_code=303)
    token_snapshot = snapshot_token(token)
    list_snapshot = snapshot_list(watch_list)
    success, detail, query_id = await attempt_graphql_repair(token_snapshot, list_snapshot)
    if success:
        token.use_fallback = True
        token.graphql_query_id = query_id
        token.healthy = True
        token.last_error = ""
        token.last_repaired_at = utc_now()
    else:
        token.healthy = False
        token.last_error = detail
    add_log(db, "INFO" if success else "ERROR", f"手动 GraphQL 修复结果: {detail}")
    return RedirectResponse(token_anchor(token_id), status_code=303)


@app.post("/tokens/{token_id}/fallback")
async def toggle_fallback(
    token_id: int,
    db: Session = Depends(get_db),
    _: str = Depends(current_user_from_cookie),
):
    token = db.query(TokenInstance).filter(TokenInstance.id == token_id).first()
    if token:
        token.use_fallback = not token.use_fallback
        token.updated_at = utc_now()
        add_log(db, "INFO", f"{token.name} fallback 状态切换为 {token.use_fallback}")
    return RedirectResponse(token_anchor(token_id), status_code=303)


@app.post("/rsshub")
async def create_rsshub(
    name: str = Form(...),
    host_port: int = Form(...),
    twitter_auth_token: str = Form(""),
    third_party_api: str = Form(""),
    proxy_uri: str = Form(""),
    db: Session = Depends(get_db),
    _: str = Depends(current_user_from_cookie),
):
    clean_name = sanitize_container_name(name)
    if not clean_name:
        add_log(db, "ERROR", "RSSHub 创建失败：名称不能为空")
        return RedirectResponse("/#rsshub", status_code=303)
    if host_port < 1 or host_port > 65535:
        add_log(db, "ERROR", "RSSHub 创建失败：端口必须在 1-65535 之间")
        return RedirectResponse("/#rsshub", status_code=303)
    selected_proxy = resolve_proxy_choice(db, proxy_uri)
    if proxy_uri and selected_proxy is None:
        add_log(db, "ERROR", "RSSHub 创建失败：请选择代理设置里已有且启用的代理")
        return RedirectResponse("/#rsshub", status_code=303)
    exists = (
        db.query(RsshubInstance)
        .filter((RsshubInstance.name == clean_name) | (RsshubInstance.host_port == host_port))
        .first()
    )
    if exists:
        add_log(db, "ERROR", "RSSHub 创建失败：名称或端口已存在")
        return RedirectResponse("/#rsshub", status_code=303)
    try:
        info = create_rsshub_container(
            clean_name,
            host_port,
            twitter_auth_token.strip(),
            third_party_api.strip(),
            selected_proxy,
        )
        status = info.status
        container_id = info.container_id
        add_log(db, "INFO", f"RSSHub 容器已创建: {clean_name}，Token 里填写 http://{clean_name}:1200")
    except DockerManagerError as exc:
        status = "create_failed"
        container_id = ""
        add_log(db, "ERROR", f"RSSHub 容器创建失败: {exc}")
    db.add(
        RsshubInstance(
            name=clean_name,
            host_port=host_port,
            internal_url=f"http://{clean_name}:1200",
            twitter_auth_token=twitter_auth_token.strip(),
            third_party_api=third_party_api.strip(),
            proxy_uri=selected_proxy,
            container_id=container_id,
            status=status,
            created_at=utc_now(),
            updated_at=utc_now(),
        )
    )
    return RedirectResponse("/#rsshub", status_code=303)


@app.post("/rsshub/discover")
async def discover_rsshub(
    db: Session = Depends(get_db),
    _: str = Depends(current_user_from_cookie),
):
    try:
        containers = list_rsshub_containers()
    except DockerManagerError as exc:
        add_log(db, "ERROR", f"RSSHub 查询失败: {exc}")
        return RedirectResponse("/#rsshub", status_code=303)

    now = utc_now()
    for container in containers:
        if not container.name or not container.host_port:
            continue
        row = db.query(RsshubInstance).filter(RsshubInstance.name == container.name).first()
        if not row:
            row = db.query(RsshubInstance).filter(RsshubInstance.host_port == container.host_port).first()
        if row:
            row.name = container.name
            row.host_port = container.host_port
            row.internal_url = container.internal_url
            row.container_id = container.container_id
            row.status = container.status
            row.updated_at = now
        else:
            db.add(
                RsshubInstance(
                    name=container.name,
                    host_port=container.host_port,
                    internal_url=container.internal_url,
                    container_id=container.container_id,
                    status=container.status,
                    created_at=now,
                    updated_at=now,
                )
            )
    add_log(db, "INFO", f"RSSHub 查询完成：当前发现 {len(containers)} 个容器")
    return RedirectResponse("/#rsshub", status_code=303)


@app.post("/rsshub/{rsshub_id}/refresh")
async def refresh_rsshub(
    rsshub_id: int,
    db: Session = Depends(get_db),
    _: str = Depends(current_user_from_cookie),
):
    item = db.query(RsshubInstance).filter(RsshubInstance.id == rsshub_id).first()
    if item and item.container_id:
        try:
            info = inspect_container(item.container_id)
            item.status = info.status
            item.updated_at = utc_now()
            add_log(db, "INFO", f"RSSHub 状态已刷新: {item.name} -> {item.status}")
        except DockerManagerError as exc:
            item.status = "unknown"
            item.updated_at = utc_now()
            add_log(db, "ERROR", f"RSSHub 状态刷新失败: {exc}")
    return RedirectResponse("/#rsshub", status_code=303)


@app.post("/rsshub/{rsshub_id}/test")
async def test_rsshub(
    rsshub_id: int,
    db: Session = Depends(get_db),
    _: str = Depends(current_user_from_cookie),
):
    item = db.query(RsshubInstance).filter(RsshubInstance.id == rsshub_id).first()
    watch_list = (
        db.query(WatchList)
        .filter(WatchList.token_id == 0, WatchList.enabled.is_(True))
        .order_by(WatchList.id.asc())
        .first()
    )
    if not item:
        add_log(db, "ERROR", "RSSHub 测试失败：实例不存在")
        return RedirectResponse("/#rsshub", status_code=303)
    if not watch_list:
        item.last_test_at = utc_now()
        item.last_test_ok = False
        item.last_test_message = "没有启用的 List，无法模拟真实 RSSHub 抓取。"
        item.updated_at = utc_now()
        add_log(db, "ERROR", f"{item.name} RSSHub 测试失败：没有启用的 List")
        return RedirectResponse("/#rsshub", status_code=303)

    ok, message = await run_rsshub_real_fetch_test(item.internal_url, watch_list.list_id)
    if not ok and item.container_id:
        log_summary = summarize_rsshub_logs(container_logs(item.container_id))
        if log_summary:
            message = f"{message}\n日志摘要：{log_summary}"
    item.last_test_at = utc_now()
    item.last_test_ok = ok
    item.last_test_message = message
    item.updated_at = utc_now()
    add_log(db, "INFO" if ok else "ERROR", f"{item.name} RSSHub 真实抓取测试: {message}")
    return RedirectResponse("/#rsshub", status_code=303)


@app.post("/rsshub/{rsshub_id}/update")
async def update_rsshub(
    rsshub_id: int,
    name: str = Form(...),
    host_port: int = Form(...),
    twitter_auth_token: str = Form(""),
    third_party_api: str = Form(""),
    proxy_uri: str = Form(""),
    db: Session = Depends(get_db),
    _: str = Depends(current_user_from_cookie),
):
    item = db.query(RsshubInstance).filter(RsshubInstance.id == rsshub_id).first()
    if not item:
        add_log(db, "ERROR", "RSSHub 更新失败：实例不存在")
        return RedirectResponse("/#rsshub", status_code=303)
    clean_name = sanitize_container_name(name)
    if not clean_name:
        add_log(db, "ERROR", "RSSHub 更新失败：名称不能为空")
        return RedirectResponse("/#rsshub", status_code=303)
    if host_port < 1 or host_port > 65535:
        add_log(db, "ERROR", "RSSHub 更新失败：端口必须在 1-65535 之间")
        return RedirectResponse("/#rsshub", status_code=303)
    selected_proxy = resolve_proxy_choice(db, proxy_uri)
    if proxy_uri and selected_proxy is None:
        add_log(db, "ERROR", "RSSHub 更新失败：请选择代理设置里已有且启用的代理")
        return RedirectResponse("/#rsshub", status_code=303)
    exists = (
        db.query(RsshubInstance)
        .filter(RsshubInstance.id != rsshub_id)
        .filter((RsshubInstance.name == clean_name) | (RsshubInstance.host_port == host_port))
        .first()
    )
    if exists:
        add_log(db, "ERROR", "RSSHub 更新失败：名称或端口已存在")
        return RedirectResponse("/#rsshub", status_code=303)
    old_name = item.name
    item.name = clean_name
    item.host_port = host_port
    item.internal_url = f"http://{clean_name}:1200"
    item.twitter_auth_token = twitter_auth_token.strip()
    item.third_party_api = third_party_api.strip()
    item.proxy_uri = selected_proxy
    item.updated_at = utc_now()
    try:
        info = recreate_rsshub_container(
            item.name,
            item.host_port,
            item.twitter_auth_token,
            item.third_party_api,
            item.proxy_uri,
            item.container_id,
        )
        item.container_id = info.container_id
        item.status = info.status
        add_log(db, "INFO", f"RSSHub 配置已更新并重建容器: {old_name} -> {item.name}:{item.host_port}")
    except DockerManagerError as exc:
        item.status = "update_failed"
        add_log(db, "ERROR", f"RSSHub 配置更新失败: {item.name} - {exc}")
    return RedirectResponse("/#rsshub", status_code=303)


@app.post("/rsshub/{rsshub_id}/delete")
async def delete_rsshub(
    rsshub_id: int,
    db: Session = Depends(get_db),
    _: str = Depends(current_user_from_cookie),
):
    item = db.query(RsshubInstance).filter(RsshubInstance.id == rsshub_id).first()
    if item:
        if item.container_id:
            try:
                remove_container(item.container_id)
                add_log(db, "INFO", f"RSSHub 容器已删除: {item.name}")
            except DockerManagerError as exc:
                add_log(db, "ERROR", f"RSSHub 容器删除失败: {exc}")
        db.delete(item)
    return RedirectResponse("/#rsshub", status_code=303)


@app.post("/lists")
async def save_list(
    name: str = Form(""),
    list_id: str = Form(...),
    db: Session = Depends(get_db),
    _: str = Depends(current_user_from_cookie),
):
    value = extract_list_id(list_id)
    old = db.query(WatchList).filter(WatchList.token_id == 0, WatchList.list_id == value).first()
    if old:
        old.name = name.strip()
        old.enabled = True
        old.subscription_checked_at = None
        old.subscription_error = ""
        db.query(TokenListState).filter(TokenListState.watch_list_id == old.id).update(
            {
                "subscription_checked_at": None,
                "subscription_error": "",
                "updated_at": utc_now(),
            }
        )
    else:
        db.add(WatchList(token_id=0, name=name.strip(), list_id=value, enabled=True))
    add_log(db, "INFO", f"List 已保存: {value}")
    return RedirectResponse("/#lists", status_code=303)


@app.post("/lists/{list_id}/toggle")
async def toggle_list(
    list_id: int,
    db: Session = Depends(get_db),
    _: str = Depends(current_user_from_cookie),
):
    item = db.query(WatchList).filter(WatchList.id == list_id).first()
    redirect_url = "/#lists"
    if item:
        item.enabled = not item.enabled
    return RedirectResponse(redirect_url, status_code=303)


@app.post("/lists/{list_id}/delete")
async def delete_list(
    list_id: int,
    db: Session = Depends(get_db),
    _: str = Depends(current_user_from_cookie),
):
    item = db.query(WatchList).filter(WatchList.id == list_id).first()
    redirect_url = "/#lists"
    if item:
        db.query(TokenListState).filter(TokenListState.watch_list_id == item.id).delete()
        db.delete(item)
    return RedirectResponse(redirect_url, status_code=303)


@app.post("/monitor/trigger")
async def trigger_monitor(_: str = Depends(current_user_from_cookie)):
    await watcher.trigger_once()
    return RedirectResponse("/#dashboard", status_code=303)


@app.get("/health")
async def health():
    return {"ok": True}


def extract_list_id(value: str) -> str:
    import re

    value = value.strip()
    match = re.search(r"/lists/(\d+)", value)
    if match:
        return match.group(1)
    match = re.search(r"(\d{5,})", value)
    return match.group(1) if match else value


def token_anchor(token_id: int) -> str:
    return f"/#token-{token_id}"


def normalize_username(value: str) -> str:
    value = value.strip()
    value = value.removeprefix("@")
    value = value.rstrip("/")
    if "/" in value:
        parts = [part for part in value.split("/") if part]
        for marker in ("x.com", "twitter.com"):
            if marker in parts:
                index = parts.index(marker)
                if len(parts) > index + 1:
                    value = parts[index + 1]
                    break
        else:
            value = parts[-1] if parts else value
    return value.lower()


def sanitize_container_name(value: str) -> str:
    import re

    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9_.-]+", "-", value)
    return value.strip("-._")


async def run_rsshub_real_fetch_test(base_url: str, list_id: str) -> tuple[bool, str]:
    url = urljoin(base_url.rstrip("/") + "/", f"twitter/list/{list_id}")
    try:
        async with httpx.AsyncClient(timeout=35.0) as client:
            resp = await client.get(url)
    except httpx.HTTPError as exc:
        return False, f"请求 RSSHub 失败：{exc}"
    if resp.status_code >= 400:
        return False, f"RSSHub HTTP {resp.status_code}: {resp.text[:300]}"
    parsed = feedparser.parse(resp.text)
    if parsed.bozo:
        return False, f"RSS 解析失败：{parsed.bozo_exception}"
    return True, f"测试成功，List {list_id} 返回 {len(parsed.entries)} 条。"


def summarize_rsshub_logs(logs: str) -> str:
    if not logs.strip():
        return ""
    keywords = (
        "Error in /twitter/list",
        "Twitter API error",
        "PROXY_URI",
        "proxy",
        "403",
        "404",
        "503",
        "ECONN",
        "ETIMEDOUT",
        "timeout",
        "Proxy",
        "Playwright",
    )
    lines = []
    for raw_line in logs.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if any(keyword in line for keyword in keywords):
            lines.append(line)
    if not lines:
        lines = [line.strip() for line in logs.splitlines() if line.strip()][-8:]
    return " | ".join(lines[-8:])[:1200]


def is_supported_proxy(proxy_url: str) -> bool:
    lowered = proxy_url.lower()
    return lowered.startswith(("http://", "https://", "socks5://", "socks5h://"))


def resolve_proxy_choice(db: Session, value: str) -> str | None:
    clean = value.strip()
    if not clean:
        return ""
    proxy = (
        db.query(ProxyProfile)
        .filter(
            ProxyProfile.proxy_url == clean,
            ProxyProfile.enabled.is_(True),
            ProxyProfile.last_test_ok.is_(True),
        )
        .first()
    )
    return proxy.proxy_url if proxy else None


async def run_proxy_test(proxy_url: str) -> tuple[bool, str]:
    clean = proxy_url.strip()
    if not is_supported_proxy(clean):
        return False, "代理格式不支持，请使用 http://、https://、socks5:// 或 socks5h://"
    transport = httpx.AsyncHTTPTransport(proxy=clean)
    async with httpx.AsyncClient(transport=transport, timeout=25.0, follow_redirects=True) as client:
        try:
            x_resp = await client.get("https://abs.twimg.com/favicons/twitter.3.ico")
            ip_resp = await client.get("https://api.ipify.org?format=json")
        except httpx.HTTPError as exc:
            return False, f"代理请求失败：{exc}"
    if x_resp.status_code >= 400:
        return False, f"代理可连接但访问 X 失败：HTTP {x_resp.status_code}"
    ip = ""
    try:
        ip = str(ip_resp.json().get("ip", ""))
    except Exception:
        ip = ip_resp.text.strip()[:80]
    return True, f"代理可用，X 静态资源 HTTP {x_resp.status_code}，出口 IP {ip or '未知'}"


def migrate_existing_proxies(db: Session) -> None:
    values = []
    values.extend(proxy for (proxy,) in db.query(TokenInstance.proxy_url).filter(TokenInstance.proxy_url != "").all())
    values.extend(proxy for (proxy,) in db.query(RsshubInstance.proxy_uri).filter(RsshubInstance.proxy_uri != "").all())
    existing = {proxy.proxy_url for proxy in db.query(ProxyProfile).all()}
    now = utc_now()
    index = len(existing) + 1
    for value in dict.fromkeys(values):
        if not value or value in existing:
            continue
        db.add(
            ProxyProfile(
                name=f"imported-proxy-{index}",
                proxy_url=value,
                enabled=True,
                last_test_ok=False,
                last_test_message="从已有 Token/RSSHub 配置迁移，尚未检测。",
                created_at=now,
                updated_at=now,
            )
        )
        existing.add(value)
        index += 1


def serialize_row(row: object, fields: list[str]) -> dict[str, object]:
    data: dict[str, object] = {}
    for field in fields:
        value = getattr(row, field)
        data[field] = value.isoformat() if isinstance(value, datetime) else value
    return data


def clean_payload(item: dict[str, object], fields: list[str]) -> dict[str, object]:
    data: dict[str, object] = {}
    for field in fields:
        if field not in item:
            continue
        value = item[field]
        if field.endswith("_at") or field == "cooldown_until":
            value = parse_datetime(value)
        data[field] = value
    return data


def parse_datetime(value: object) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return None


def beijing_now() -> datetime:
    return utc_now().astimezone(BEIJING_TZ)


def build_stability_chart(logs: list[Log]) -> dict[str, object]:
    now = beijing_now()
    start = now - timedelta(hours=24)
    slots = 24
    chart_height = 46
    points: list[dict[str, object]] = []
    total_score = 0

    for index in range(slots):
        slot_start = start + timedelta(hours=index)
        slot_end = slot_start + timedelta(hours=1)
        slot_logs = [
            log for log in logs
            if slot_start <= ensure_beijing(log.created_at) < slot_end
        ]
        errors = sum(1 for log in slot_logs if log.level.upper() == "ERROR")
        warnings = sum(1 for log in slot_logs if log.level.upper() == "WARNING")
        score = max(0, 100 - errors * 18 - warnings * 8)
        total_score += score
        x = round(index * (100 / (slots - 1)), 2)
        y = round((100 - score) * (chart_height / 100), 2)
        points.append(
            {
                "x": x,
                "y": y,
                "top": round(y * 100 / chart_height, 2),
                "score": score,
                "label": slot_start.strftime("%H:%M"),
                "detail": f"{slot_start.strftime('%H:%M')} - 稳定度 {score}% / 错误 {errors} / 警告 {warnings}",
                "errors": errors,
                "warnings": warnings,
                "offline": score < 95,
            }
        )

    average = round(total_score / slots, 2) if slots else 100
    return {
        "availability": average,
        "points": points,
        "line_path": smooth_svg_path(points),
        "labels": [
            points[index]["label"]
            for index in (0, 4, 8, 12, 16, 20, 23)
            if index < len(points)
        ],
    }


def smooth_svg_path(points: list[dict[str, object]]) -> str:
    if not points:
        return ""
    path = f"M {points[0]['x']} {points[0]['y']}"
    for index in range(1, len(points)):
        prev = points[index - 1]
        current = points[index]
        control_x = round((float(prev["x"]) + float(current["x"])) / 2, 2)
        path += f" C {control_x} {prev['y']}, {control_x} {current['y']}, {current['x']} {current['y']}"
    return path


def ensure_beijing(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(BEIJING_TZ)


def format_beijing_time(value: object) -> str:
    if not value:
        return "暂无"
    if isinstance(value, str):
        value = parse_datetime(value)
    if not isinstance(value, datetime):
        return str(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")
