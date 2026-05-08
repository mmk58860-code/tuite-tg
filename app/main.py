from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager

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
    aliases = db.query(UserAlias).order_by(UserAlias.username.asc()).all()
    logs = db.query(Log).order_by(Log.id.desc()).limit(120).all()
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
        "last_checked_at": last_checked,
        "telegram_ready": bool(settings["telegram_bot_token"] and settings["telegram_chat_id"]),
    }
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "tokens": tokens,
            "lists": lists,
            "logs": logs,
            "settings": settings,
            "stats": stats,
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
    return RedirectResponse("/", status_code=303)


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
    return RedirectResponse("/", status_code=303)


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
        return RedirectResponse("/", status_code=303)

    try:
        raw = await backup_file.read()
        payload = json.loads(raw.decode("utf-8-sig"))
        if payload.get("app") != "tuite-tg":
            raise ValueError("不是 Tuite TG 备份文件")
    except Exception as exc:
        add_log(db, "ERROR", f"导入失败：备份文件无法读取 ({exc})")
        return RedirectResponse("/", status_code=303)

    try:
        db.query(TokenListState).delete()
        db.query(SeenItem).delete()
        db.query(WatchList).delete()
        db.query(TokenInstance).delete()
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

        for item in payload.get("user_aliases", []):
            db.add(UserAlias(**clean_payload(item, USER_ALIAS_EXPORT_FIELDS)))

        add_log(db, "INFO", "数据导入完成，已覆盖 Token、List、用户备注、系统配置和去重记录")
    except Exception as exc:
        db.rollback()
        add_log(db, "ERROR", f"导入失败：{exc}")
    return RedirectResponse("/", status_code=303)


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
    redirect_url = "/#tokens"
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
        token.proxy_url = proxy_url.strip()
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
            proxy_url=proxy_url.strip(),
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
    return RedirectResponse("/#tokens", status_code=303)


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
        return RedirectResponse("/#tokens", status_code=303)
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
    return RedirectResponse("/", status_code=303)


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


def format_beijing_time(value: object) -> str:
    if not value:
        return "暂无"
    if isinstance(value, str):
        value = parse_datetime(value)
    if not isinstance(value, datetime):
        return str(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S 北京时间")
