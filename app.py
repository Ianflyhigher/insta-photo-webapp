# -*- coding: utf-8 -*-
r"""
인스타그램 공개 계정 사진 다운로더 — 로컬/클라우드 겸용 웹앱

[동작 방식]
  1. 아이디를 넣으면 게시물을 '전부' 긁지 않고, 12개씩 소량만 목록으로 보여 줍니다.
  2. 목록에서 원하는 사진만 골라 원본 해상도로 받거나, 고른 것만 ZIP으로 받습니다.
  3. 받은 이력(shortcode 기준)을 data/history.json 에 남겨, 다음에 볼 때
     '다운 완료'로 표시하고 중복 다운로드를 건너뜁니다.
  4. 인스타 차단을 피하려고 요청 사이에 1.5~3초 무작위 대기, 목록 캐시,
     연속 조회 쿨다운을 넣었습니다.
  5. 로그인 없이 막히는 경우(요즘 흔함)를 위해 sessionid 쿠키 입력을 지원합니다.

[실행]
  · 이 PC:      실행하기.bat 더블클릭  (또는 python app.py)
  · 클라우드:    gunicorn app:app        (Procfile / render.yaml 참고)

[환경변수 — 모두 선택]
  PORT, DATA_DIR, ACCESS_CODE, INSTA_SESSIONID, INSTA_CSRFTOKEN,
  INSTA_PAUSE_MIN, INSTA_PAUSE_MAX, NO_SLEEP=1(대기 끄기), NO_BROWSER=1
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import random
import re
import socket
import threading
import time
import uuid
import webbrowser
import zipfile
from datetime import datetime
from pathlib import Path

from flask import (Flask, abort, jsonify, redirect, render_template, request,
                   send_file, session, url_for)

from instaloader import Instaloader, Profile
from instaloader import exceptions as il_exceptions
from instaloader.exceptions import (
    ConnectionException,
    InstaloaderException,
    LoginRequiredException,
    ProfileNotExistsException,
    QueryReturnedBadRequestException,
    QueryReturnedForbiddenException,
    QueryReturnedNotFoundException,
    TooManyRequestsException,
)

# ---------------------------------------------------------------------------
# 경로 · 상수
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
# 클라우드에서 디스크를 따로 붙일 때는 DOWNLOADS_DIR 로 바꿀 수 있습니다.
DOWNLOAD_ROOT = Path(os.environ.get("DOWNLOADS_DIR") or (BASE_DIR / "downloads"))
DATA_DIR = Path(os.environ.get("DATA_DIR") or (BASE_DIR / "data"))
CACHE_DIR = DATA_DIR / "cache"
LISTING_CACHE_DIR = CACHE_DIR / "listings"
THUMB_CACHE_DIR = CACHE_DIR / "thumbs"
ZIP_DIR = CACHE_DIR / "zips"
HISTORY_FILE = DATA_DIR / "history.json"

for folder in (DOWNLOAD_ROOT, CACHE_DIR, LISTING_CACHE_DIR, THUMB_CACHE_DIR, ZIP_DIR):
    folder.mkdir(parents=True, exist_ok=True)

BATCH = 12                 # 처음/한 번에 보여 줄 사진 수
MAX_ITEMS = 120            # 한 계정에서 한 번에 목록에 담아 둘 최대 사진 수
LISTING_TTL = 20 * 60      # 목록 캐시 유효 시간(초)
COOLDOWN = 10              # 새 목록을 연달아 요청할 때 기다릴 시간(초)
PAUSE_MIN, PAUSE_MAX = 1.5, 3.0   # 인스타 요청 사이 무작위 대기(초)

USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,40}$")
KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")

ACCESS_CODE = os.environ.get("ACCESS_CODE", "").strip()

_PRIVATE_ERRORS = tuple(
    getattr(il_exceptions, name)
    for name in ("PrivateProfileException", "PrivateProfileNotFollowedException")
    if hasattr(il_exceptions, name)
) or (InstaloaderException,)

app = Flask(__name__)
app.json.ensure_ascii = False
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024


def _secret_key() -> str:
    key = os.environ.get("SECRET_KEY", "").strip()
    if key:
        return key
    path = DATA_DIR / "secret.key"
    try:
        if path.exists():
            return path.read_text("utf-8").strip()
        value = uuid.uuid4().hex + uuid.uuid4().hex
        path.write_text(value, "utf-8")
        return value
    except OSError:
        return uuid.uuid4().hex


app.secret_key = _secret_key()


class FriendlyError(Exception):
    """화면에 그대로 보여 줄 한국어 안내."""

    def __init__(self, message: str, hint: str = "", title: str = "", need_login: bool = False):
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.title = title
        self.need_login = need_login


# ---------------------------------------------------------------------------
# 인스타 요청 예절: 무작위 대기 (차단 방지)
# ---------------------------------------------------------------------------

def pause_gate(lo: float | None = None, hi: float | None = None) -> None:
    """인스타그램에 요청을 보내기 전에 1.5~3초 쉬어 갑니다."""
    if os.environ.get("NO_SLEEP") == "1":
        return
    low = float(os.environ.get("INSTA_PAUSE_MIN", lo if lo is not None else PAUSE_MIN))
    high = float(os.environ.get("INSTA_PAUSE_MAX", hi if hi is not None else PAUSE_MAX))
    if high < low:
        high = low
    time.sleep(random.uniform(low, high))


# ---------------------------------------------------------------------------
# 다운로드 이력 (data/history.json) — shortcode 기준 중복 방지
# ---------------------------------------------------------------------------

_HIST_LOCK = threading.RLock()
_HISTORY: dict = {"version": 1, "downloads": {}}


def history_load() -> None:
    global _HISTORY
    with _HIST_LOCK:
        _HISTORY = {"version": 1, "downloads": {}}
        if HISTORY_FILE.exists():
            try:
                data = json.loads(HISTORY_FILE.read_text("utf-8"))
                if isinstance(data, dict) and isinstance(data.get("downloads"), dict):
                    _HISTORY = data
            except (ValueError, OSError) as exc:
                print(f"[이력] history.json 을 읽지 못했습니다: {exc}")
        _HISTORY.setdefault("downloads", {})


def history_save() -> None:
    with _HIST_LOCK:
        try:
            tmp = HISTORY_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(_HISTORY, ensure_ascii=False, indent=1), "utf-8")
            tmp.replace(HISTORY_FILE)
        except OSError as exc:
            print(f"[이력] 저장 실패: {exc}")


def history_get(key: str) -> dict | None:
    with _HIST_LOCK:
        rec = _HISTORY["downloads"].get(key)
        return dict(rec) if rec else None


def history_mark(key: str, record: dict) -> None:
    with _HIST_LOCK:
        _HISTORY["downloads"][key] = record
        history_save()


def history_accounts() -> list[dict]:
    """이력에서 계정별 요약을 만듭니다(실제 파일이 남아 있는 것만 셉니다)."""
    summary: dict[str, dict] = {}
    with _HIST_LOCK:
        records = list(_HISTORY["downloads"].items())
    for key, rec in records:
        user = rec.get("username") or ""
        path = rec.get("path") or ""
        if not user or not path or not Path(path).exists():
            continue
        row = summary.setdefault(user, {"username": user, "count": 0, "size": 0, "latest_ts": 0.0})
        row["count"] += 1
        row["size"] += int(rec.get("size") or 0)
        row["latest_ts"] = max(row["latest_ts"], float(rec.get("ts") or 0))
    rows = []
    for row in summary.values():
        ts = row.pop("latest_ts")
        row["latest"] = datetime.fromtimestamp(ts).strftime("%m월 %d일 %H:%M") if ts else ""
        row["size_mb"] = round(row["size"] / (1024 * 1024), 1)
        rows.append(row)
    rows.sort(key=lambda r: r["count"], reverse=True)
    return rows


history_load()


def cleanup_caches() -> None:
    """오래된 임시 파일(ZIP·썸네일)을 정리해 디스크가 넘치지 않게 합니다."""
    now = time.time()
    for folder, age in ((ZIP_DIR, 6 * 3600), (THUMB_CACHE_DIR, 7 * 24 * 3600)):
        for path in folder.glob("*"):
            try:
                if path.is_file() and now - path.stat().st_mtime > age:
                    path.unlink()
            except OSError:
                pass


cleanup_caches()

# ---------------------------------------------------------------------------
# 목록 캐시
# ---------------------------------------------------------------------------

_CACHE_LOCK = threading.RLock()
_LISTING_CACHE_MEM: dict[str, tuple[float, dict]] = {}


def listing_cache_path(username: str) -> Path:
    return LISTING_CACHE_DIR / f"{username}.json"


def listing_cache_read(username: str) -> dict | None:
    path = listing_cache_path(username)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    with _CACHE_LOCK:
        cached = _LISTING_CACHE_MEM.get(username)
        if cached and cached[0] == mtime:
            return cached[1]
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    with _CACHE_LOCK:
        _LISTING_CACHE_MEM[username] = (mtime, data)
    return data


def listing_cache_write(username: str, listing: dict) -> None:
    payload = {
        "username": username,
        "profile": listing["profile"],
        "items": listing["items"],
        "scanned": listing["scanned"],
        "exhausted": listing["exhausted"],
        "fetched_at": listing["fetched_at"],
    }
    path = listing_cache_path(username)
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
        tmp.replace(path)
    except OSError as exc:
        print(f"[캐시] 저장 실패: {exc}")
        return
    with _CACHE_LOCK:
        _LISTING_CACHE_MEM.pop(username, None)


def item_lookup(username: str, key: str) -> dict | None:
    data = listing_cache_read(username)
    if not data:
        return None
    for item in data.get("items", []):
        if item.get("key") == key:
            return item
    return None


# ---------------------------------------------------------------------------
# 세션(로그인 쿠키) — 메모리에만 보관, 화면에는 마스킹해서 표시
# ---------------------------------------------------------------------------

SESSION_CFG: dict = {"sessionid": "", "csrftoken": "", "ds_user_id": "", "username": "", "set_at": 0.0}
SESSION_LOCK = threading.RLock()
SESSION_META: dict = {"ok": False, "checked": 0.0}


def parse_cookie_text(text: str) -> dict:
    """'sessionid=...; csrftoken=...' 또는 sessionid 값만 붙여넣어도 받아 줍니다."""
    text = (text or "").strip()
    if not text:
        raise FriendlyError("세션 값을 입력해 주세요.")
    parts = {}
    if "=" in text:
        for chunk in re.split(r"[;\n\r]+", text):
            if "=" in chunk:
                k, v = chunk.split("=", 1)
                parts[k.strip().lower()] = v.strip().strip('"')
    else:
        parts["sessionid"] = text
    sid = parts.get("sessionid", "")
    if not sid or len(sid) < 20 or not re.match(r"^[A-Za-z0-9_%@:.\-]+$", sid):
        raise FriendlyError(
            "sessionid 값이 올바르지 않습니다.",
            "브라우저에서 복사한 값이 맞는지 확인해 주세요. 보통 30자 이상입니다.",
        )
    return {
        "sessionid": sid,
        "csrftoken": parts.get("csrftoken", ""),
        "ds_user_id": parts.get("ds_user_id", ""),
    }


def session_cookie() -> dict | None:
    """우선순위: 화면에서 입력한 값 → 환경변수 → session-* 파일."""
    with SESSION_LOCK:
        if SESSION_CFG["sessionid"]:
            return dict(SESSION_CFG)
    sid = os.environ.get("INSTA_SESSIONID", "").strip()
    if sid:
        return {
            "sessionid": sid,
            "csrftoken": os.environ.get("INSTA_CSRFTOKEN", "").strip(),
            "ds_user_id": os.environ.get("INSTA_DS_USER_ID", "").strip(),
            "username": "",
            "set_at": 0.0,
        }
    return None


def session_status() -> dict:
    with SESSION_LOCK:
        cfg = dict(SESSION_CFG)
    if cfg["sessionid"]:
        sid = cfg["sessionid"]
        return {
            "configured": True,
            "source": "직접 입력",
            "username": cfg.get("username") or "",
            "masked": f"sessionid …{sid[-4:]}",
        }
    sid = os.environ.get("INSTA_SESSIONID", "").strip()
    if sid:
        return {"configured": True, "source": "환경변수", "username": "", "masked": f"sessionid …{sid[-4:]}"}
    users = find_session_files()
    if users:
        return {"configured": True, "source": "세션 파일", "username": users[0], "masked": f"session-{users[0]}"}
    return {"configured": False, "source": "없음", "username": "", "masked": ""}


def find_session_files() -> list[str]:
    users = []
    for path in sorted(BASE_DIR.glob("session-*")):
        name = path.name[len("session-"):]
        if path.is_file() and USERNAME_RE.match(name):
            users.append(name)
    return users


# ---------------------------------------------------------------------------
# Instaloader 준비
# ---------------------------------------------------------------------------

def build_loader() -> tuple[Instaloader, str | None, str]:
    """(loader, 세션 사용자, 안내문) 을 돌려줍니다."""
    loader = Instaloader(
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        compress_json=False,
        post_metadata_txt_pattern="",
        max_connection_attempts=2,
        request_timeout=30.0,
        quiet=True,
        sleep=True,
    )
    note = ""
    cookie = session_cookie()
    if cookie:
        data = {"sessionid": cookie["sessionid"]}
        csrf = cookie.get("csrftoken") or ""
        if not csrf:
            # csrftoken 이 없으면 임의 값을 넣어 줍니다(읽기 전용 요청에는 충분).
            csrf = hashlib.md5(os.urandom(16)).hexdigest()
        data["csrftoken"] = csrf
        if cookie.get("ds_user_id"):
            data["ds_user_id"] = cookie["ds_user_id"]
        try:
            loader.context.load_session(cookie.get("ds_user_id") or "session_user", data)
        except Exception as exc:
            note = f"세션 쿠키를 적용하지 못했습니다. ({exc})"
            return loader, None, note
        return loader, cookie.get("username") or "session", note

    users = find_session_files()
    if users:
        try:
            loader.load_session_from_file(users[0])
            return loader, users[0], note
        except Exception as exc:
            note = f"session-{users[0]} 파일을 읽지 못해 로그인 없이 시도합니다. ({exc})"
    return loader, None, note


def verify_session() -> dict:
    """입력된 세션이 실제로 어느 계정인지 확인합니다."""
    loader, user, note = build_loader()
    if not user:
        return {"ok": False, "username": "", "error": note or "세션이 설정되어 있지 않습니다."}
    pause_gate()
    try:
        name = loader.test_login()
    except Exception as exc:
        return {"ok": False, "username": "", "error": describe_error(exc).message}
    if name:
        with SESSION_LOCK:
            SESSION_CFG["username"] = name
            SESSION_META["ok"] = True
            SESSION_META["checked"] = time.time()
        return {"ok": True, "username": name, "error": ""}
    return {
        "ok": False,
        "username": "",
        "error": "쿠키가 만료되었거나 올바르지 않은 것 같습니다. 인스타그램에서 다시 로그인한 뒤 새 sessionid를 복사해 주세요.",
    }


# ---------------------------------------------------------------------------
# 오류 → 한국어 안내
# ---------------------------------------------------------------------------

def describe_error(exc: Exception) -> FriendlyError:
    if isinstance(exc, FriendlyError):
        return exc
    status = getattr(exc, "http_status", 0) or 0
    text = str(exc) or exc.__class__.__name__

    if isinstance(exc, ProfileNotExistsException):
        return FriendlyError(
            "그런 아이디의 계정을 찾을 수 없습니다.",
            "철자를 확인해 주세요. 계정을 삭제했거나 이름을 바꾼 경우에도 이렇게 나옵니다.",
            "계정을 찾을 수 없음",
        )
    if isinstance(exc, _PRIVATE_ERRORS):
        return FriendlyError(
            "비공개 계정이라 사진을 볼 수 없습니다.",
            "이 앱은 공개 계정만 지원합니다. 상대가 공개로 바꾸면 볼 수 있습니다.",
            "비공개 계정",
        )
    if isinstance(exc, getattr(il_exceptions, "ProfileHasNoPicsException", ())):
        return FriendlyError(
            "이 계정에는 사진 게시물이 없습니다.",
            "영상만 올린 계정이거나 게시물이 비어 있을 수 있습니다.",
            "사진 없음",
        )
    if isinstance(exc, TooManyRequestsException) or status == 429 or "429" in text:
        return FriendlyError(
            "인스타그램이 요청을 너무 많이 받았다며 잠시 막았습니다. (429)",
            "15~30분 기다렸다가 다시 시도해 주세요. 같은 아이디로 연달아 누르면 더 오래 막힙니다.",
            "잠시 차단됨",
        )
    if isinstance(exc, LoginRequiredException) or status == 401:
        return FriendlyError(
            "인스타그램이 로그인을 요구하고 있습니다. (401)",
            "요즘 인스타그램은 로그인 없이 보는 길을 거의 막아 두었습니다. "
            "아래 [인스타그램 로그인 세션]에 sessionid를 한 번만 넣어 두면 해결됩니다.",
            "로그인 필요",
            need_login=True,
        )
    if isinstance(exc, QueryReturnedForbiddenException) or status == 403:
        return FriendlyError(
            "인스타그램이 접근을 거부했습니다. (403)",
            "잠시 뒤 다시 시도하거나 [인스타그램 로그인 세션]을 설정해 보세요. "
            "같은 아이디로 너무 자주 누르면 차단이 길어집니다.",
            "접근 거부",
            need_login=True,
        )
    if isinstance(exc, QueryReturnedNotFoundException) or status == 404:
        return FriendlyError(
            "인스타그램에서 해당 페이지를 찾지 못했습니다. (404)",
            "아이디를 다시 확인해 주세요. 잠시 뒤 다시 시도하면 될 때도 있습니다.",
            "찾을 수 없음",
        )
    if isinstance(exc, QueryReturnedBadRequestException) or status == 400:
        return FriendlyError(
            "인스타그램이 요청을 거절했습니다. (400)",
            "아이디를 확인하고 잠시 뒤 다시 시도해 주세요. 계속되면 세션을 설정해 보세요.",
            "요청 거절",
        )
    if isinstance(exc, ConnectionException):
        return FriendlyError(
            "인스타그램 서버와 연결하지 못했습니다.",
            "인터넷 연결을 확인하고 잠시 뒤 다시 시도해 주세요. "
            "사무실·학교 와이파이처럼 인스타그램이 막힌 곳에서는 안 될 수 있습니다.",
            "연결 실패",
        )
    if isinstance(exc, InstaloaderException):
        return FriendlyError(
            "인스타그램에서 정보를 가져오지 못했습니다.",
            f"잠시 뒤 다시 시도해 주세요. 계속되면 세션을 설정해 보세요. (내용: {text[:200]})",
            "가져오기 실패",
        )
    return FriendlyError(
        "알 수 없는 문제가 생겼습니다.",
        f"잠시 뒤 다시 시도해 주세요. (내용: {text[:200]})",
        "오류",
    )


# ---------------------------------------------------------------------------
# 목록(리스트업) 만들기 — 12개씩, 필요할 때만
# ---------------------------------------------------------------------------

LISTINGS: dict[str, dict] = {}
LISTING_LOCK = threading.RLock()
_LAST_NEW: dict[str, float] = {}


def normalize_username(raw: str) -> str:
    text = (raw or "").strip()
    m = re.search(r"instagram\.com/([A-Za-z0-9._]+)", text, re.IGNORECASE)
    if m:
        text = m.group(1)
    text = text.strip().lstrip("@").strip().strip("/").lower()
    if not text:
        raise FriendlyError("인스타그램 아이디를 입력해 주세요.")
    if not USERNAME_RE.match(text):
        raise FriendlyError(
            "아이디 형식이 올바르지 않습니다.",
            "영문, 숫자, 마침표(.), 밑줄(_)만 쓸 수 있습니다. 예) nasa",
        )
    return text


def ext_for(url: str) -> str:
    path = (url or "").split("?")[0].lower()
    for ext in (".webp", ".png", ".heic", ".jpeg", ".jpg"):
        if path.endswith(ext):
            return "jpg" if ext == ".jpeg" else ext.lstrip(".")
    return "jpg"


def post_to_items(post) -> list[dict]:
    """게시물 하나에서 '사진' 항목들만 뽑아냅니다(영상 제외)."""
    shortcode = getattr(post, "shortcode", "") or ""
    if not re.match(r"^[A-Za-z0-9_-]+$", shortcode):
        return []
    stamp = getattr(post, "date_utc", None)
    if isinstance(stamp, datetime):
        date_txt = stamp.strftime("%Y-%m-%d %H:%M")
        file_date = stamp.strftime("%Y-%m-%d_%H-%M")
    else:
        date_txt = str(stamp)[:16] if stamp else ""
        file_date = date_txt.replace(" ", "_").replace(":", "-")

    caption = " ".join(((getattr(post, "caption", "") or "").split()))
    caption = caption[:90]

    urls: list[str] = []
    typename = getattr(post, "typename", "")
    if typename == "GraphSidecar":
        try:
            for node in post.get_sidecar_nodes():
                if not getattr(node, "is_video", False):
                    urls.append(node.display_url)
        except Exception as exc:      # 일부 게시물을 못 읽어도 나머지는 계속
            print(f"[건너뜀] {shortcode}: {exc}")
            return []
    elif not getattr(post, "is_video", False):
        urls.append(post.url)

    items = []
    total = len(urls)
    for idx, url in enumerate(urls):
        key = shortcode if total == 1 else f"{shortcode}_{idx + 1}"
        name = f"{file_date}_{shortcode}" + ("" if total == 1 else f"_{idx + 1}") + f".{ext_for(url)}"
        items.append({
            "key": key,
            "shortcode": shortcode,
            "index": idx,
            "date": date_txt,
            "caption": caption,
            "src": url,
            "fname": name,
        })
    return items


def profile_info(profile) -> dict:
    return {
        "username": profile.username,
        "full_name": profile.full_name or "",
        "followers": int(getattr(profile, "followers", 0) or 0),
        "posts": int(getattr(profile, "mediacount", 0) or 0),
        "is_private": bool(getattr(profile, "is_private", False)),
        "pic": getattr(profile, "profile_pic_url", "") or "",
    }


def new_listing(username: str) -> dict:
    """인스타그램에서 계정 정보를 새로 가져옵니다(요청 1회)."""
    left = cooldown_left(username)
    if left:
        raise FriendlyError(
            f"인스타그램 보호를 위해 {left}초 뒤에 다시 불러올 수 있습니다.",
            "연달아 새로 불러오면 인스타그램이 차단할 수 있어 조금씩만 요청합니다.",
            "잠시만 기다려 주세요",
        )
    loader, session_user, note = build_loader()
    pause_gate()
    profile = Profile.from_username(loader.context, username)
    info = profile_info(profile)
    if info["is_private"] and not session_user:
        raise FriendlyError(
            "비공개 계정입니다.",
            "이 앱은 공개 계정의 사진만 볼 수 있습니다.",
            "비공개 계정",
        )
    listing = {
        "username": username,
        "profile": info,
        "items": [],
        "scanned": 0,
        "exhausted": False,
        "fetched_at": time.time(),
        "profile_obj": profile,
        "iterator": None,
        "loader": loader,
        "session": session_user,
        "note": note,
        "lock": threading.RLock(),
        "capped": False,
    }
    _LAST_NEW[username] = time.time()
    listing_cache_write(username, listing)
    return listing


def cooldown_left(username: str) -> int:
    last = _LAST_NEW.get(username)
    if not last:
        return 0
    return max(0, int(COOLDOWN - (time.time() - last)))


def ensure_listing(username: str, refresh: bool = False) -> dict:
    now = time.time()
    with LISTING_LOCK:
        current = LISTINGS.get(username)
        if current and not refresh and now - current["fetched_at"] < LISTING_TTL:
            return current

    if not refresh:
        cached = listing_cache_read(username)
        if cached and now - float(cached.get("fetched_at") or 0) < LISTING_TTL:
            listing = {
                "username": username,
                "profile": cached.get("profile") or {},
                "items": cached.get("items") or [],
                "scanned": int(cached.get("scanned") or 0),
                "exhausted": bool(cached.get("exhausted")),
                "fetched_at": float(cached.get("fetched_at") or now),
                "profile_obj": None,
                "iterator": None,
                "loader": None,
                "session": None,
                "note": "",
                "lock": threading.RLock(),
                "capped": len(cached.get("items") or []) >= MAX_ITEMS,
            }
            with LISTING_LOCK:
                LISTINGS[username] = listing
            return listing

    listing = new_listing(username)
    with LISTING_LOCK:
        LISTINGS[username] = listing
    return listing


def ensure_items(listing: dict, want_total: int) -> None:
    """목록에 사진 항목이 want_total 개가 될 때까지(또는 끝까지) 게시물을 조금씩 읽습니다."""
    with listing["lock"]:
        if listing["capped"]:
            listing["exhausted"] = True
        if listing["iterator"] is None and not listing["exhausted"]:
            if listing["profile_obj"] is None or listing["loader"] is None:
                loader, session_user, note = build_loader()
                listing["loader"] = loader
                listing["session"] = listing["session"] or session_user
                listing["note"] = listing["note"] or note
                pause_gate()
                listing["profile_obj"] = Profile.from_username(
                    loader.context, listing["username"]
                )
            listing["iterator"] = listing["profile_obj"].get_posts()
            # 이미 읽은 게시물은 건너뜁니다.
            skipped_posts = 0
            while skipped_posts < listing["scanned"]:
                if skipped_posts and skipped_posts % 12 == 0:
                    pause_gate()
                try:
                    next(listing["iterator"])
                except StopIteration:
                    listing["exhausted"] = True
                    break
                skipped_posts += 1

        guard = 0
        while (len(listing["items"]) < want_total and not listing["exhausted"]
               and len(listing["items"]) < MAX_ITEMS):
            guard += 1
            if guard > 4000:                 # 안전장치(무한 루프 방지)
                listing["exhausted"] = True
                break
            if listing["scanned"] and listing["scanned"] % 12 == 0:
                pause_gate()                 # 페이지 단위로 한 번씩 쉬어 갑니다
            elif listing["scanned"] == 0:
                pause_gate()
            try:
                post = next(listing["iterator"])
            except StopIteration:
                listing["exhausted"] = True
                break
            listing["scanned"] += 1
            listing["items"].extend(post_to_items(post))

        if len(listing["items"]) >= MAX_ITEMS:
            listing["capped"] = True
        listing_cache_write(listing["username"], listing)


def public_item(item: dict, username: str) -> dict:
    rec = history_get(item["key"])
    downloaded = bool(rec and rec.get("path") and Path(rec["path"]).exists())
    return {
        "key": item["key"],
        "shortcode": item["shortcode"],
        "date": item["date"],
        "caption": item["caption"],
        "thumb": item["src"],
        "thumb_url": f"/thumb/{item['key']}?u={username}",
        "download_url": f"/download/{item['key']}?u={username}",
        "downloaded": downloaded,
    }


def listing_payload(listing: dict, start: int, end: int | None = None) -> dict:
    items = listing["items"]
    end = len(items) if end is None else min(end, len(items))
    username = listing["username"]
    return {
        "items": [public_item(it, username) for it in items[start:end]],
        "loaded": len(items),
        "has_more": (not listing["exhausted"] and len(items) < MAX_ITEMS),
        "exhausted": listing["exhausted"],
        "capped": listing["capped"],
    }


# ---------------------------------------------------------------------------
# 다운로드 작업
# ---------------------------------------------------------------------------

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


def job_update(job_id: str, **fields) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job:
            job.update(fields)


def job_snapshot(job_id: str) -> dict | None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return None
        data = {k: v for k, v in job.items() if not k.startswith("_")}
        return data


def run_download_job(job_id: str) -> None:
    with JOBS_LOCK:
        job = dict(JOBS.get(job_id) or {})
    if not job:
        return
    username = job["username"]
    items = job.get("_items") or []
    folder = DOWNLOAD_ROOT / username
    folder.mkdir(parents=True, exist_ok=True)
    files: list[dict] = []
    errors = 0
    skipped = 0

    try:
        loader, session_user, note = build_loader()
        total = len(items)
        for idx, item in enumerate(items):
            key = item["key"]
            job_update(job_id, message=f"{idx + 1}/{total} · {item['fname']}", percent=int(idx / max(total, 1) * 100))

            rec = history_get(key)
            path = folder / item["fname"]
            if rec and Path(rec.get("path") or "").exists():
                skipped += 1
                files.append({"key": key, "name": item["fname"],
                              "url": f"/download/{key}?u={username}",
                              "size": int(rec.get("size") or 0)})
                continue

            try:
                pause_gate()
                data = loader.context.get_raw(item["src"]).content
            except (TooManyRequestsException, LoginRequiredException,
                    QueryReturnedForbiddenException) as exc:
                raise exc
            except Exception as exc:
                errors += 1
                print(f"[실패] {item['fname']}: {exc}")
                continue

            if not data:
                errors += 1
                continue
            path.write_bytes(data)
            history_mark(key, {
                "username": username,
                "shortcode": item["shortcode"],
                "index": item.get("index", 0),
                "path": str(path),
                "fname": item["fname"],
                "size": len(data),
                "date": item["date"],
                "caption": item["caption"],
                "ts": time.time(),
                "downloaded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
            files.append({"key": key, "name": item["fname"],
                          "url": f"/download/{key}?u={username}",
                          "size": len(data)})
            job_update(job_id, files=list(files), done=len(files))

        if not files:
            raise FriendlyError(
                "사진을 받지 못했습니다.",
                "잠시 뒤 다시 시도해 주세요. 계속되면 세션을 설정해 보세요.",
                "다운로드 실패",
            )

        msg = f"사진 {len(files)}장을 받았습니다."
        if skipped:
            msg += f" (이미 받은 {skipped}장은 건너뜀)"
        if errors:
            msg += f" · {errors}장 실패"
        job_update(job_id, state="done", percent=100, message=msg, files=files,
                   zip_url=f"/zip/job/{job_id}", errors=errors, skipped=skipped)
        print(f"[완료] @{username}: {msg}")

    except Exception as exc:
        friendly = describe_error(exc)
        job_update(
            job_id,
            state="error",
            error=friendly.message,
            error_hint=friendly.hint,
            error_title=friendly.title or "다운로드 실패",
            need_login=friendly.need_login,
            files=files,
            zip_url=f"/zip/job/{job_id}" if files else "",
            message="중단되었습니다.",
        )
        print(f"[오류] @{username}: {friendly.message} / {exc}")


def build_zip_from_files(files: list[dict], username: str) -> Path | None:
    pairs = []
    for f in files:
        rec = history_get(f["key"])
        path = Path(rec["path"]) if rec and rec.get("path") else None
        if path and path.exists():
            pairs.append((path, f["name"]))
    if not pairs:
        return None
    target = ZIP_DIR / f"{username}_{uuid.uuid4().hex[:8]}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
        for path, name in pairs:
            zf.write(path, arcname=f"{username}/{name}")
    return target


# ---------------------------------------------------------------------------
# 라우트
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return jsonify(ok=True, time=datetime.now().isoformat(timespec="seconds"))


@app.before_request
def access_gate():
    """ACCESS_CODE 를 설정했을 때만 동작하는 간단한 접속 암호."""
    if not ACCESS_CODE:
        return None
    if request.path in ("/health", "/login"):
        return None
    expected = hashlib.sha256((ACCESS_CODE + app.secret_key).encode()).hexdigest()
    if session.get("auth") == expected:
        return None
    if request.path.startswith("/api/") or request.path.startswith("/download") \
            or request.path.startswith("/zip") or request.path.startswith("/thumb"):
        return jsonify(ok=False, error="접속 암호가 필요합니다.", login=True), 401
    return redirect(url_for("login_page", next=request.path))


@app.get("/login")
def login_page():
    return render_template("login.html", error="")


@app.post("/login")
def login_submit():
    code = (request.form.get("code") or "").strip()
    if code and code == ACCESS_CODE:
        session["auth"] = hashlib.sha256((ACCESS_CODE + app.secret_key).encode()).hexdigest()
        nxt = request.args.get("next") or "/"
        if not nxt.startswith("/") or nxt.startswith("//"):
            nxt = "/"
        return redirect(nxt)
    return render_template("login.html", error="암호가 맞지 않습니다."), 401


@app.get("/")
def index():
    return render_template("index.html", accounts=history_accounts(), status=session_status())


@app.get("/api/session")
def api_session():
    return jsonify(ok=True, status=session_status())


@app.post("/api/session")
def api_session_set():
    data = request.get_json(silent=True) or {}
    try:
        parsed = parse_cookie_text(str(data.get("cookie", "")))
    except FriendlyError as exc:
        return jsonify(ok=False, error=exc.message, hint=exc.hint), 400
    with SESSION_LOCK:
        SESSION_CFG.update(parsed)
        SESSION_CFG["username"] = ""
        SESSION_CFG["set_at"] = time.time()
    check = verify_session()
    return jsonify(ok=True, verified=check["ok"], username=check.get("username", ""),
                   error=check.get("error", ""), status=session_status())


@app.post("/api/session/clear")
def api_session_clear():
    with SESSION_LOCK:
        SESSION_CFG.update({"sessionid": "", "csrftoken": "", "ds_user_id": "", "username": "", "set_at": 0.0})
    return jsonify(ok=True, status=session_status())


@app.post("/api/list")
def api_list():
    payload = request.get_json(silent=True) or {}
    refresh = bool(payload.get("refresh"))
    try:
        username = normalize_username(str(payload.get("username", "")))
    except FriendlyError as exc:
        return jsonify(ok=False, error=exc.message, hint=exc.hint), 400
    try:
        listing = ensure_listing(username, refresh=refresh)
        ensure_items(listing, BATCH)
    except FriendlyError as exc:
        return jsonify(ok=False, error=exc.message, hint=exc.hint,
                       title=exc.title, need_login=exc.need_login), 200
    except Exception as exc:
        friendly = describe_error(exc)
        return jsonify(ok=False, error=friendly.message, hint=friendly.hint,
                       title=friendly.title, need_login=friendly.need_login), 200

    if not listing["items"]:
        return jsonify(
            ok=False,
            error="사진이 있는 게시물을 찾지 못했습니다.",
            hint="최근 게시물이 모두 영상이거나 비어 있는 계정일 수 있습니다.",
            title="사진 없음",
        ), 200

    body = listing_payload(listing, 0, BATCH)
    body.update({
        "ok": True,
        "username": username,
        "profile": listing["profile"],
        "session": listing["session"],
        "note": listing["note"],
        "status": session_status(),
    })
    return jsonify(body)


@app.post("/api/more")
def api_more():
    payload = request.get_json(silent=True) or {}
    try:
        username = normalize_username(str(payload.get("username", "")))
    except FriendlyError as exc:
        return jsonify(ok=False, error=exc.message), 400
    listing = LISTINGS.get(username) or ensure_listing(username)
    start = int(payload.get("start") or len(listing["items"]))
    try:
        ensure_items(listing, max(start + BATCH, len(listing["items"])))
    except Exception as exc:
        friendly = describe_error(exc)
        body = listing_payload(listing, start)
        body.update({"ok": True, "username": username, "profile": listing["profile"],
                     "warning": friendly.message, "warning_hint": friendly.hint,
                     "need_login": friendly.need_login})
        return jsonify(body)
    body = listing_payload(listing, start)
    body.update({"ok": True, "username": username, "profile": listing["profile"]})
    return jsonify(body)


@app.post("/api/download")
def api_download():
    payload = request.get_json(silent=True) or {}
    try:
        username = normalize_username(str(payload.get("username", "")))
    except FriendlyError as exc:
        return jsonify(ok=False, error=exc.message), 400
    keys = payload.get("keys") or []
    if not isinstance(keys, list) or not keys:
        return jsonify(ok=False, error="받을 사진을 먼저 선택해 주세요."), 400
    keys = [str(k) for k in keys if KEY_RE.match(str(k))][:MAX_ITEMS]
    if not keys:
        return jsonify(ok=False, error="선택한 사진이 올바르지 않습니다."), 400

    items = []
    for key in keys:
        item = item_lookup(username, key)
        if item:
            items.append(item)
    if not items:
        return jsonify(ok=False,
                       error="사진 목록이 만료되었습니다.",
                       hint="아이디를 다시 불러온 뒤 선택해 주세요."), 200

    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {
            "id": job_id,
            "kind": "download",
            "username": username,
            "keys": [it["key"] for it in items],
            "_items": items,
            "state": "running",
            "message": "준비 중…",
            "percent": 1,
            "files": [],
            "done": 0,
            "total": len(items),
            "zip_url": "",
            "started": datetime.now().strftime("%H:%M:%S"),
        }
        if len(JOBS) > 40:
            for old in sorted(JOBS.values(), key=lambda j: j.get("started", ""))[:-40]:
                JOBS.pop(old["id"], None)

    threading.Thread(target=run_download_job, args=(job_id,), daemon=True).start()
    return jsonify(ok=True, job_id=job_id, total=len(items))


@app.get("/api/job/<job_id>")
def api_job(job_id: str):
    job = job_snapshot(job_id)
    if not job:
        return jsonify(ok=False, error="작업을 찾을 수 없습니다. 다시 시도해 주세요."), 404
    return jsonify(ok=True, **job)


@app.get("/api/accounts")
def api_accounts():
    return jsonify(ok=True, accounts=history_accounts())


@app.get("/api/item/<key>")
def api_item(key: str):
    if not KEY_RE.match(key):
        abort(404)
    username = (request.args.get("u") or "").strip().lower()
    if not USERNAME_RE.match(username):
        abort(404)
    item = item_lookup(username, key)
    if not item:
        return jsonify(ok=False, error="목록이 만료되었습니다. 아이디를 다시 불러와 주세요."), 404
    return jsonify(ok=True, item=public_item(item, username))


@app.get("/thumb/<key>")
def thumb(key: str):
    if not KEY_RE.match(key):
        abort(404)
    username = (request.args.get("u") or "").strip().lower()
    if not USERNAME_RE.match(username):
        abort(404)
    cached = THUMB_CACHE_DIR / f"{key}.jpg"
    if cached.exists():
        return send_file(cached, mimetype="image/jpeg", max_age=3600)
    item = item_lookup(username, key)
    if not item:
        abort(404)
    try:
        pause_gate(0.3, 0.8)
        loader, _, _ = build_loader()
        data = loader.context.get_raw(item["src"]).content
        cached.write_bytes(data)
        return send_file(io.BytesIO(data), mimetype="image/jpeg", max_age=3600)
    except Exception as exc:
        print(f"[썸네일 실패] {key}: {exc}")
        abort(404)


@app.get("/download/<key>")
def download_one(key: str):
    if not KEY_RE.match(key):
        abort(404)
    username = (request.args.get("u") or "").strip().lower()
    if not USERNAME_RE.match(username):
        abort(404)

    rec = history_get(key)
    if rec and Path(rec.get("path") or "").exists():
        return send_file(rec["path"], as_attachment=True, download_name=rec.get("fname") or f"{key}.jpg")

    item = item_lookup(username, key)
    if not item:
        abort(404)
    folder = DOWNLOAD_ROOT / username
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / item["fname"]
    if not path.exists():
        pause_gate()
        loader, _, _ = build_loader()
        path.write_bytes(loader.context.get_raw(item["src"]).content)
    size = path.stat().st_size
    history_mark(key, {
        "username": username,
        "shortcode": item["shortcode"],
        "index": item.get("index", 0),
        "path": str(path),
        "fname": item["fname"],
        "size": size,
        "date": item["date"],
        "caption": item["caption"],
        "ts": time.time(),
        "downloaded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    return send_file(path, as_attachment=True, download_name=item["fname"])


@app.get("/zip/job/<job_id>")
def zip_job(job_id: str):
    job = job_snapshot(job_id)
    if not job or not job.get("files"):
        abort(404)
    path = build_zip_from_files(job["files"], job["username"])
    if not path:
        abort(404)
    return send_file(path, as_attachment=True,
                     download_name=f"instagram_{job['username']}_{datetime.now():%Y%m%d_%H%M}.zip",
                     mimetype="application/zip")


@app.get("/zip/<username>")
def zip_account(username: str):
    username = (username or "").strip().lower()
    if not USERNAME_RE.match(username):
        abort(404)
    with _HIST_LOCK:
        keys = [k for k, rec in _HISTORY["downloads"].items() if rec.get("username") == username]
    files = [{"key": k, "name": (history_get(k) or {}).get("fname") or f"{k}.jpg"} for k in keys]
    path = build_zip_from_files(files, username)
    if not path:
        abort(404)
    return send_file(path, as_attachment=True,
                     download_name=f"instagram_{username}_{datetime.now():%Y%m%d_%H%M}.zip",
                     mimetype="application/zip")


# ---------------------------------------------------------------------------
# 실행 (로컬)
# ---------------------------------------------------------------------------

def local_ips() -> list[str]:
    ips: list[str] = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.6)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            ips.append(ip)
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127.") and not ip.startswith("169.254."):
                ips.append(ip)
    except OSError:
        pass
    return ips


def free_port(start: int) -> int:
    for port in range(start, start + 10):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("0.0.0.0", port))
                return port
            except OSError:
                continue
    raise SystemExit("사용할 수 있는 포트를 찾지 못했습니다. PC를 다시 시작한 뒤 실행해 주세요.")


def banner(port: int) -> None:
    line = "=" * 60
    ips = local_ips()
    print("\n" + line)
    print("  인스타 사진 다운로더가 실행 중입니다.")
    print(line)
    print(f"  이 컴퓨터에서 :  http://localhost:{port}")
    if ips:
        for ip in ips:
            print(f"  폰·노트북에서 :  http://{ip}:{port}")
    else:
        print(f"  폰·노트북에서 :  http://(이 PC의 IP):{port}")
        print("                  (IP는 명령 프롬프트에서 ipconfig 로 확인)")
    print(line)
    print("  · 폰과 PC가 같은 와이파이에 있어야 합니다.")
    print("  · 처음 접속할 때 윈도우 방화벽 창이 뜨면 [액세스 허용]을 눌러 주세요.")
    print("  · 받은 사진은 이 폴더의 downloads\\<아이디>\\ 에 저장됩니다.")
    print("  · 끌 때는 이 창에서 Ctrl + C 를 누르거나 창을 닫으면 됩니다.")
    print(line + "\n")


if __name__ == "__main__":
    port = free_port(int(os.environ.get("PORT", "5000")))
    banner(port)
    if os.environ.get("NO_BROWSER") != "1":
        try:
            threading.Timer(1.5, lambda: webbrowser.open(f"http://localhost:{port}")).start()
        except Exception:
            pass
    try:
        app.run(host="0.0.0.0", port=port, threaded=True, debug=False, use_reloader=False)
    except KeyboardInterrupt:
        print("\n종료합니다.")
