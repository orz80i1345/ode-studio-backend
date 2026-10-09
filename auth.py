#!/usr/bin/env python3
import argparse
import asyncio
import html
import json
import os
import re
import secrets
import sys
from datetime import datetime
from http.cookiejar import CookieJar
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
from urllib.request import HTTPCookieProcessor, Request, build_opener
from zoneinfo import ZoneInfo

import websockets


AUTH_URL = "https://manager.tanmolock.com/auth"
WS_URL = "wss://ws.tanmolock.com/ws"
ORIGIN = "https://manager.tanmolock.com"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/150.0.0.0 Safari/537.36"
)
TAIPEI_TZ = ZoneInfo("Asia/Taipei")
DEBUG_PAGE = Path(__file__).with_name("tanmo-auth-page.html")


def parse_booking_datetime(value):
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}-\d{2}", value):
            raise ValueError
        date = datetime.strptime(value, "%Y-%m-%d-%H").replace(tzinfo=TAIPEI_TZ)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "時間必須是 YYYY-MM-DD-HH 格式，且小時介於 00–23，"
            f"例如 2026-10-05-10（收到：{value}）"
        ) from exc
    return int(date.timestamp())


def format_booking_datetime(timestamp):
    return datetime.fromtimestamp(timestamp, TAIPEI_TZ).strftime("%Y-%m-%d-%H")


def parse_args():
    parser = argparse.ArgumentParser(
        description="登入 TanmoLock，並可選擇透過 WebSocket 新增預約密碼。"
    )
    parser.add_argument(
        "--book",
        action="store_true",
        help="實際新增預約；未指定時只登入並檢查裝置 token。",
    )
    parser.add_argument(
        "--begin",
        type=parse_booking_datetime,
        metavar="YYYY-MM-DD-HH",
        help="預約開始時間（台北時間，例如 2026-10-05-10）。",
    )
    parser.add_argument(
        "--end",
        type=parse_booking_datetime,
        metavar="YYYY-MM-DD-HH",
        help="預約結束時間（台北時間，例如 2026-10-05-18）。",
    )
    parser.add_argument("--remark", default="", help="預約備註。")
    parser.add_argument(
        "--device-id",
        type=int,
        default=int(os.environ.get("TANMO_DEVICE_ID", "0")),
        help="WebSocket 裝置 id，預設 0。",
    )
    args = parser.parse_args()

    if args.book:
        missing = [
            name
            for name, value in (
                ("--begin", args.begin),
                ("--end", args.end),
            )
            if value is None
        ]
        if missing:
            parser.error("使用 --book 時必須提供 " + "、".join(missing))
        if args.begin >= args.end:
            parser.error("--begin 必須早於 --end")
    return args


def browser_headers():
    return {
        "User-Agent": USER_AGENT,
        "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7,ja;q=0.6",
    }


def open_text(opener, request, timeout=15):
    with opener.open(request, timeout=timeout) as response:
        return response.geturl(), response.read().decode("utf-8", errors="replace")


def login(username, password):
    cookie_jar = CookieJar()
    opener = build_opener(HTTPCookieProcessor(cookie_jar))
    headers = browser_headers()

    # 先取得 PHPSESSID，再用同一個 CookieJar 登入。
    open_text(
        opener,
        Request(
            AUTH_URL,
            headers={
                **headers,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        ),
    )

    body = urlencode(
        {"method": "auth", "username": username, "password": password}
    ).encode("utf-8")
    _, response_text = open_text(
        opener,
        Request(
            AUTH_URL,
            data=body,
            headers={
                **headers,
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Accept": "*/*",
                "Origin": ORIGIN,
                "Referer": AUTH_URL,
                "X-Requested-With": "XMLHttpRequest",
            },
            method="POST",
        ),
    )
    result = json.loads(response_text)
    if not isinstance(result, dict) or not isinstance(result.get("info"), str):
        raise ValueError("登入 response 缺少字串欄位 info")
    if result.get("status") != 1:
        raise ValueError("登入失敗：" + result["info"])

    # 原網站登入成功後會 location.reload()；token 在重載後 menu.html 的 URL。
    page_url, page_html = open_text(
        opener,
        Request(
            AUTH_URL,
            headers={
                **headers,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": AUTH_URL,
            },
        ),
    )
    token = extract_device_token(page_html, page_url)
    if not token:
        DEBUG_PAGE.write_text(page_html, encoding="utf-8")
        DEBUG_PAGE.chmod(0o600)
        raise ValueError(
            "登入成功，但在登入後頁面找不到 token。已將頁面保存至 "
            f"{DEBUG_PAGE}，請勿公開此檔案。登入後最終 URL：{page_url}"
        )
    return result["info"], token, cookie_jar


class DeviceTokenParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.token = None

    def handle_starttag(self, tag, attrs):
        if self.token or tag.lower() != "input":
            return
        attributes = dict(attrs)
        classes = attributes.get("class", "").split()
        is_token_input = (
            "token" in classes
            or attributes.get("name") == "token"
            or attributes.get("id") == "token"
        )
        value = attributes.get("value", "")
        if is_token_input and len(value) >= 20:
            self.token = value


def extract_device_token(page_html, page_url):
    decoded = html.unescape(page_html)

    # 登入後頁面目前使用：<input class="token" value="TOKEN" type="hidden">。
    parser = DeviceTokenParser()
    parser.feed(decoded)
    if parser.token:
        return parser.token

    # 一般形式：<iframe src=".../menu.html?...&token=TOKEN">。
    for match in re.finditer(r"(?:src|href)\s*=\s*([\"'])(.*?)\1", decoded, re.I | re.S):
        candidate = urljoin(page_url, match.group(2))
        parsed = urlparse(candidate)
        values = parse_qs(parsed.query).get("token")
        if values and ("menu.html" in parsed.path or len(values[0]) >= 20):
            return values[0]

    # 相容由 JavaScript 組出的 menu.html URL。
    match = re.search(
        r"menu\.html[^\"'<>]*?[?&]token=([^&\"'<>\s]+)", decoded, re.I
    )
    if match:
        return match.group(1)
    return None


def cookie_header(cookie_jar):
    return "; ".join(f"{cookie.name}={cookie.value}" for cookie in cookie_jar)


def decode_message(raw):
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("WebSocket 訊息不是 JSON object")
    return value


async def receive_until(ws, wanted_func, timeout=20):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError(f"等待 {wanted_func} 逾時")
        message = decode_message(await asyncio.wait_for(ws.recv(), remaining))
        func = message.get("func")
        if func == "ping":
            await ws.send(json.dumps({"func": "ping"}, separators=(",", ":")))
            continue
        if func == wanted_func:
            return message
        if message.get("status") == 0 or func in {"error", "send_error"}:
            raise ValueError("WebSocket 回報錯誤：" + json.dumps(message, ensure_ascii=False))


async def websocket_booking(token, cookie_jar, args):
    headers = {}
    cookies = cookie_header(cookie_jar)
    if cookies:
        headers["Cookie"] = cookies

    async with websockets.connect(
        WS_URL,
        origin=ORIGIN,
        additional_headers=headers,
        user_agent_header=USER_AGENT,
        open_timeout=15,
        ping_interval=20,
        ping_timeout=20,
    ) as ws:
        hello = {
            "func": "web_dv_client",
            "devices": [{"id": args.device_id, "token": token}],
            "stamp": 0,
        }
        await ws.send(json.dumps(hello, ensure_ascii=False, separators=(",", ":")))

        # 等待現有預約同步完成，避免初始化尚未完成就送 add_wb_rpd。
        await receive_until(ws, "send_wb_rpd")
        print("WebSocket 已連線，裝置資料同步完成。", file=sys.stderr)

        if not args.book:
            return

        door_pass = f"{secrets.randbelow(10000):04d}"
        reservation = {
            "func": "add_wb_rpd",
            "did": args.device_id,
            "beg": args.begin,
            "end": args.end,
            "pass": door_pass,
            "remark": args.remark,
        }
        await ws.send(
            json.dumps(reservation, ensure_ascii=False, separators=(",", ":"))
        )
        response = {
            "password": door_pass,
            "remark": args.remark,
            "begin": format_booking_datetime(args.begin),
            "end": format_booking_datetime(args.end),
        }
        print(json.dumps(response, ensure_ascii=False))


def main():
    args = parse_args()
    username = os.environ.get("TANMO_USERNAME", "odestudio")
    password = os.environ.get("TANMO_PASSWORD")
    if not password:
        print("請設定環境變數 TANMO_PASSWORD", file=sys.stderr)
        return 1

    try:
        info, token, cookie_jar = login(username, password)
        print(info, file=sys.stderr)
        print(f"已取得裝置 token：{token[:6]}…{token[-4:]}", file=sys.stderr)
        asyncio.run(websocket_booking(token, cookie_jar, args))
        return 0
    except HTTPError as exc:
        print(f"HTTP 錯誤：{exc.code}，URL：{exc.url}", file=sys.stderr)
        print(exc.read(2000).decode("utf-8", errors="replace"), file=sys.stderr)
    except (URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"請求失敗：{exc}", file=sys.stderr)
    except websockets.exceptions.WebSocketException as exc:
        print(f"WebSocket 失敗：{exc}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
