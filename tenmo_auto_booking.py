#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import base64
import html
import json
import os
import re
import secrets
import sys
from datetime import datetime
from http.cookiejar import CookieJar
from html.parser import HTMLParser
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
from urllib.request import HTTPCookieProcessor, Request, build_opener
from zoneinfo import ZoneInfo

import websockets


AUTH_URL = "https://manager.tanmolock.com/auth"
WS_URL = "wss://ws.tanmolock.com/ws"
ORIGIN = "https://manager.tanmolock.com"
USER_AGENT = "Mozilla/5.0 (AWS Lambda) AppleWebKit/537.36 Chrome/150 Safari/537.36"
TAIPEI_TZ = ZoneInfo("Asia/Taipei")


class RequestValidationError(ValueError):
    pass


def parse_booking_datetime(value: str) -> int:
    try:
        if not isinstance(value, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}-\d{2}", value
        ):
            raise ValueError
        date = datetime.strptime(value, "%Y-%m-%d-%H").replace(tzinfo=TAIPEI_TZ)
    except ValueError as exc:
        raise RequestValidationError(
            "時間必須是 YYYY-MM-DD-HH 格式，且小時介於 00–23，"
            f"例如 2026-10-05-10（收到：{value}）"
        ) from exc
    return int(date.timestamp())


def format_booking_datetime(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, TAIPEI_TZ).strftime("%Y-%m-%d-%H")


def browser_headers() -> dict[str, str]:
    return {
        "User-Agent": USER_AGENT,
        "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7,ja;q=0.6",
    }


def open_text(opener: Any, request: Request, timeout: int = 15) -> tuple[str, str]:
    with opener.open(request, timeout=timeout) as response:
        return response.geturl(), response.read().decode("utf-8", errors="replace")


def login(username: str, password: str) -> tuple[str, str, CookieJar]:
    cookie_jar = CookieJar()
    opener = build_opener(HTTPCookieProcessor(cookie_jar))
    headers = browser_headers()

    # Obtain PHPSESSID first, then authenticate with the same cookie jar.
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

    _, response_text = open_text(
        opener,
        Request(
            AUTH_URL,
            data=urlencode(
                {"method": "auth", "username": username, "password": password}
            ).encode("utf-8"),
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

    # The web app reloads after login; the device token is in the reloaded page.
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
        raise ValueError("登入成功，但在登入後頁面找不到裝置 token")
    return result["info"], token, cookie_jar


class DeviceTokenParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.token: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.token or tag.lower() != "input":
            return
        attributes = dict(attrs)
        classes = (attributes.get("class") or "").split()
        is_token_input = (
            "token" in classes
            or attributes.get("name") == "token"
            or attributes.get("id") == "token"
        )
        value = attributes.get("value") or ""
        if is_token_input and len(value) >= 20:
            self.token = value


def extract_device_token(page_html: str, page_url: str) -> str | None:
    decoded = html.unescape(page_html)

    parser = DeviceTokenParser()
    parser.feed(decoded)
    if parser.token:
        return parser.token

    for match in re.finditer(r"(?:src|href)\s*=\s*([\"'])(.*?)\1", decoded, re.I | re.S):
        candidate = urljoin(page_url, match.group(2))
        parsed = urlparse(candidate)
        values = parse_qs(parsed.query).get("token")
        if values and ("menu.html" in parsed.path or len(values[0]) >= 20):
            return values[0]

    match = re.search(
        r"menu\.html[^\"'<>]*?[?&]token=([^&\"'<>\s]+)", decoded, re.I
    )
    return match.group(1) if match else None


def cookie_header(cookie_jar: CookieJar) -> str:
    return "; ".join(f"{cookie.name}={cookie.value}" for cookie in cookie_jar)


def decode_message(raw: str | bytes) -> dict[str, Any]:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("WebSocket 訊息不是 JSON object")
    return value


async def receive_until(ws: Any, wanted_func: str, timeout: int = 20) -> dict[str, Any]:
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
            raise ValueError(
                "WebSocket 回報錯誤：" + json.dumps(message, ensure_ascii=False)
            )


async def websocket_booking(
    token: str,
    cookie_jar: CookieJar,
    device_id: int,
    begin: int,
    end: int,
    remark: str,
) -> dict[str, Any]:
    headers: dict[str, str] = {}
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
        await ws.send(
            json.dumps(
                {
                    "func": "web_dv_client",
                    "devices": [{"id": device_id, "token": token}],
                    "stamp": 0,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )

        # Wait for the initial reservation sync before adding a new one.
        await receive_until(ws, "send_wb_rpd")

        door_password = f"{secrets.randbelow(10000):04d}"
        await ws.send(
            json.dumps(
                {
                    "func": "add_wb_rpd",
                    "did": device_id,
                    "beg": begin,
                    "end": end,
                    "pass": door_password,
                    "remark": remark,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )

        return {
            "password": door_password,
            "remark": remark,
            "begin": format_booking_datetime(begin),
            "end": format_booking_datetime(end),
            "device_id": device_id,
        }


def parse_event(event: dict[str, Any]) -> tuple[int, int, int, str]:
    raw_body: Any = event.get("body", event)
    if event.get("isBase64Encoded") and isinstance(raw_body, str):
        raw_body = base64.b64decode(raw_body).decode("utf-8")
    if isinstance(raw_body, str):
        try:
            body = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise RequestValidationError("body 必須是合法 JSON") from exc
    else:
        body = raw_body
    if not isinstance(body, dict):
        raise RequestValidationError("body 必須是 JSON object")

    begin = parse_booking_datetime(body.get("begin"))
    end = parse_booking_datetime(body.get("end"))
    if begin >= end:
        raise RequestValidationError("begin 必須早於 end")

    raw_device_id = body.get("device_id", os.environ.get("TANMO_DEVICE_ID", "0"))
    try:
        device_id = int(raw_device_id)
    except (TypeError, ValueError) as exc:
        raise RequestValidationError("device_id 必須是整數") from exc
    if device_id < 0:
        raise RequestValidationError("device_id 不可小於 0")

    remark = body.get("remark", "")
    if not isinstance(remark, str):
        raise RequestValidationError("remark 必須是字串")
    return device_id, begin, end, remark


def api_response(
    status_code: int,
    body: dict[str, Any],
    event: dict[str, Any] | None = None,
) -> dict[str, Any]:
    allowed_origins = {
        origin.strip()
        for origin in os.environ.get("CORS_ALLOW_ORIGINS", "*").split(",")
        if origin.strip()
    }
    request_headers = (event or {}).get("headers") or {}
    request_origin = request_headers.get("origin") or request_headers.get("Origin")
    allow_origin = (
        "*"
        if "*" in allowed_origins
        else request_origin
        if request_origin in allowed_origins
        else next(iter(allowed_origins), "null")
    )
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json; charset=utf-8",
            "Access-Control-Allow-Origin": allow_origin,
        },
        "body": json.dumps(body, ensure_ascii=False),
        "isBase64Encoded": False,
    }


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    del context
    try:
        username = os.environ.get("TANMO_USERNAME", "odestudio")
        password = os.environ.get("TANMO_PASSWORD")
        if not password:
            raise RuntimeError("Lambda environment variable TANMO_PASSWORD is missing")

        device_id, begin, end, remark = parse_event(event)
        login_info, token, cookie_jar = login(username, password)
        booking = asyncio.run(
            websocket_booking(token, cookie_jar, device_id, begin, end, remark)
        )
        return api_response(
            201,
            {"ok": True, "message": login_info, "booking": booking},
            event,
        )
    except RequestValidationError as exc:
        return api_response(400, {"ok": False, "message": str(exc)}, event)
    except HTTPError as exc:
        print(f"Tanmo HTTP error: status={exc.code} url={exc.url}", file=sys.stderr)
        return api_response(502, {"ok": False, "message": "Tanmo HTTP 請求失敗"}, event)
    except (URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Tanmo request failed: {exc}", file=sys.stderr)
        return api_response(502, {"ok": False, "message": "Tanmo 請求失敗"}, event)
    except websockets.exceptions.WebSocketException as exc:
        print(f"Tanmo WebSocket failed: {exc}", file=sys.stderr)
        return api_response(502, {"ok": False, "message": "Tanmo WebSocket 失敗"}, event)
    except Exception as exc:
        print(f"Unexpected booking error: {exc}", file=sys.stderr)
        return api_response(500, {"ok": False, "message": "Internal server error"}, event)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="透過 Tanmo WebSocket 新增預約密碼。")
    parser.add_argument("--begin", required=True, help="YYYY-MM-DD-HH（台北時間）")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD-HH（台北時間）")
    parser.add_argument("--remark", default="")
    parser.add_argument("--device-id", type=int)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    event = {
        "begin": args.begin,
        "end": args.end,
        "remark": args.remark,
    }
    if args.device_id is not None:
        event["device_id"] = args.device_id
    result = lambda_handler(event, None)
    print(result["body"])
    return 0 if result["statusCode"] == 201 else 1


if __name__ == "__main__":
    sys.exit(main())
