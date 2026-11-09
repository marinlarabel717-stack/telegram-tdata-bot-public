"""Session -> official Telegram Web login URLs, without logging bearer links."""
import asyncio
import shutil
import stat
import sqlite3
import threading
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit, parse_qs

API_ID = 2040
API_HASH = "b18441a1ff607e10a989891a5462e627"
MAX_UPLOAD = 100 * 1024 * 1024
MAX_EXTRACTED = 300 * 1024 * 1024
MAX_SESSIONS = 200
_active = set()
_lock = threading.Lock()


def claim(user_id):
    with _lock:
        if user_id in _active:
            return False
        _active.add(user_id)
        return True


def release(user_id):
    with _lock:
        _active.discard(user_id)


def collect_sessions(upload, destination):
    upload, destination = Path(upload), Path(destination)
    if upload.suffix.lower() == ".session":
        return [upload]
    if upload.suffix.lower() != ".zip":
        raise ValueError("Upload a .session file or a ZIP containing sessions")
    destination.mkdir(parents=True, exist_ok=True)
    sessions = []
    with zipfile.ZipFile(upload) as archive:
        entries = archive.infolist()
        if len(entries) > 10000 or sum(item.file_size for item in entries) > MAX_EXTRACTED:
            raise ValueError("Archive exceeds extraction limits")
        for item in entries:
            name = item.filename.replace(chr(92), "/")
            path = PurePosixPath(name)
            mode = item.external_attr >> 16
            if path.is_absolute() or ".." in path.parts or ":" in name or stat.S_ISLNK(mode):
                raise ValueError("Unsafe archive path")
            if item.is_dir() or "__MACOSX" in path.parts or path.suffix.lower() != ".session":
                continue
            if len(sessions) >= MAX_SESSIONS:
                raise ValueError("At most 200 sessions per upload")
            # Flatten to unique internal names; archive names cannot escape or collide.
            target = destination / (str(len(sessions)) + ".session")
            with archive.open(item) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output, length=65536)
            sessions.append((target, path.as_posix()))
    return sessions


def accepted_url(value):
    if not isinstance(value, str) or any(ch in value for ch in "\r\n"):
        return False
    try:
        parsed = urlsplit(value)
        fields = parse_qs(parsed.fragment)
        return (parsed.scheme == "https" and parsed.netloc == "web.telegram.org"
                and parsed.path in ("/", "/a/") and bool(fields.get("tgWebAuthToken")))
    except ValueError:
        return False


async def request_token(client, url, functions, types):
    try:
        request = functions.messages.RequestUrlAuthRequest(url=url)
    except TypeError:
        request = functions.messages.RequestUrlAuthRequest(
            peer=types.InputPeerEmpty(), msg_id=0, button_id=0, url=url)
    return await client(request)


class WebSessionError(ValueError):
    """Controlled, credential-free error safe for the private report."""


def load_memory_session(path):
    """Read auth fields by name, avoiding SDK SQLite schema/version migrations."""
    from telethon.sessions import MemorySession
    from telethon.crypto import AuthKey
    try:
        connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(sessions)")}
            required = {"dc_id", "server_address", "port", "auth_key"}
            if not required.issubset(columns):
                if {"dc_id", "auth_key", "api_id"}.issubset(columns):
                    raise WebSessionError("检测到 Pyrogram session，请先转换为 Telethon session")
                raise WebSessionError("session 数据结构不受支持，缺少 Telethon 授权字段")
            row = connection.execute("SELECT dc_id, server_address, port, auth_key FROM sessions LIMIT 1").fetchone()
            if row is None:
                raise WebSessionError("session 中没有已登录账号的授权数据")
            key = row["auth_key"]
            if not isinstance(key, bytes) or len(key) != 256:
                raise WebSessionError("session 授权密钥缺失或格式无效")
            dc_id, address, port = int(row["dc_id"]), row["server_address"], int(row["port"])
            if dc_id <= 0 or not isinstance(address, str) or not address or not 0 < port <= 65535:
                raise WebSessionError("session 的数据中心连接参数无效")
        finally:
            connection.close()
    except sqlite3.DatabaseError:
        raise WebSessionError("无法读取 session 数据库：文件损坏、非 SQLite 格式或正在被占用") from None
    except (TypeError, ValueError) as error:
        if isinstance(error, WebSessionError):
            raise
        raise WebSessionError("session 授权字段格式无效") from None
    memory = MemorySession()
    memory.set_dc(dc_id, address, port)
    memory.auth_key = AuthKey(data=key)
    return memory


def describe_error(error, stage):
    if isinstance(error, WebSessionError):
        return str(error)
    names = {"load": "读取session", "init": "创建客户端", "connect": "连接Telegram",
             "authorize": "检查登录状态", "token": "请求Web链接"}
    hints = {"ValueError": "会话结构或连接参数不兼容", "TimeoutError": "请求超时，请检查代理或网络",
             "AuthKeyUnregisteredError": "session 授权已失效", "SessionRevokedError": "session 已被撤销",
             "UserDeactivatedBanError": "账号已被封禁", "FloodWaitError": "Telegram 限流，请稍后重试"}
    kind = type(error).__name__
    if is_flood_error(error):
        seconds = getattr(error, "seconds", None)
        wait = f"，至少等待 {seconds} 秒后再试" if isinstance(seconds, int) and seconds > 0 else "，Telegram 未给出等待时长，请稍后再试"
        return names.get(stage, stage) + "：Telegram 限流（" + kind + "）" + wait + "；已停止该账号后续请求，不自动重试"
    return names.get(stage, stage) + "：" + kind + ("（" + hints[kind] + "）" if kind in hints else "")


def is_flood_error(error):
    return any(base.__name__ in {"FloodError", "FloodWaitError", "FloodPremiumWaitError"}
               for base in type(error).__mro__) or getattr(error, "code", None) == 420


def response_failure(result, types):
    if isinstance(result, getattr(types, "UrlAuthResultDefault", ())):
        return "Telegram 返回 UrlAuthResultDefault：本次未授权生成直连链接，可尝试浏览器扫码登录"
    if isinstance(result, getattr(types, "UrlAuthResultRequest", ())):
        return "Telegram 返回 UrlAuthResultRequest：需要额外授权或浏览器交互，未生成直连链接"
    if isinstance(result, types.UrlAuthResultAccepted):
        return "Telegram 已接受请求，但返回链接缺少有效 Web 登录 token；不能作为直连链接"
    return "Telegram 返回未识别的授权结果；未生成直连链接"


async def generate_link(session, label, proxy=None, client_factory=None, session_loader=None):
    from telethon import TelegramClient, functions, types
    factory = client_factory or TelegramClient
    client = None
    stage = "load"
    label = str(label).replace("\r", " ").replace("\n", " ")
    try:
        authorized_session = (session_loader or load_memory_session)(session)
        stage = "init"
        client = factory(authorized_session, API_ID, API_HASH, proxy=proxy,
                         connection_retries=1, request_retries=0, timeout=15,
                         flood_sleep_threshold=0)
        async def work():
            nonlocal stage
            stage = "connect"
            await asyncio.wait_for(client.connect(), 25)
            stage = "authorize"
            if not await client.is_user_authorized():
                return {"name": label, "error": "Session is not authorized：当前 session 未授权或授权已失效，需要重新登录后导出有效 session"}
            last_error = None
            stage = "token"
            for url in ("https://web.telegram.org/a/", "https://web.telegram.org/"):
                try:
                    result = await asyncio.wait_for(request_token(client, url, functions, types), 25)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    last_error = describe_error(error, stage)
                    if is_flood_error(error):
                        return {"name": label, "error": last_error}
                    continue
                value = getattr(result, "url", None)
                if isinstance(result, types.UrlAuthResultAccepted) and accepted_url(value):
                    return {"name": label, "url": value}
                last_error = response_failure(result, types)
            return {"name": label, "error": last_error or "Telegram 未返回可用的 Web 登录链接"}
        return await asyncio.wait_for(work(), 80)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        # Exception messages can contain credentials/URLs; return only the class.
        return {"name": label, "error": describe_error(error, stage)}
    finally:
        if client is not None:
            try:
                await asyncio.wait_for(client.disconnect(), 5)
            except (Exception, asyncio.CancelledError):
                pass


def render_txt(results):
    lines = ["Web direct login / web直连", "请复制每个账号的完整链接到浏览器地址栏打开。",
             "链接短时有效，请尽快使用；失效后重新生成。请勿转发登录链接。", ""]
    for item in results:
        lines.extend(["账号 / Account: " + item["name"], item.get("url") or "失败 / Failed: " + item.get("error", "Unknown"), ""])
    return "\n".join(lines)
