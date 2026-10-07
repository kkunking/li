from __future__ import annotations

import json
import os
import sys
import time
from http import HTTPStatus
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = Path(os.environ.get("WORKBUDDY_STATE_PATH", str(ROOT / "playwright_state.json")))
BASE_URL = os.environ.get("WORKBUDDY_BASE_URL", "https://www.workbuddy.cn").rstrip("/")
STATUS_PATH = os.environ.get("WORKBUDDY_STATUS_PATH", "/billing/meter/checkin-activity-status")
CHECKIN_PATH = os.environ.get("WORKBUDDY_CHECKIN_PATH", "/billing/meter/daily-checkin")


class CheckinError(RuntimeError):
    pass


def load_state() -> dict:
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CheckinError(
            f"找不到登录状态: {STATE_PATH}。先在可登录的机器上运行 --setup，再复制该文件。"
        ) from exc
    except json.JSONDecodeError as exc:
        raise CheckinError(f"登录状态不是有效 JSON: {STATE_PATH}") from exc
    if not isinstance(state.get("cookies"), list) or not state["cookies"]:
        raise CheckinError("登录状态中没有 cookies，无法执行服务器签到")
    return state


def cookie_header(state: dict) -> str:
    cookies = []
    for cookie in state["cookies"]:
        name, value = cookie.get("name"), cookie.get("value")
        domain = cookie.get("domain", "")
        expires = cookie.get("expires", -1)
        if expires not in (None, -1) and expires > 0 and expires <= time.time():
            continue
        if name and value is not None and (not domain or "workbuddy.cn" in domain):
            cookies.append(f"{name}={value}")
    if not cookies:
        raise CheckinError("登录状态中没有 WorkBuddy cookies")
    return "; ".join(cookies)


def find_value(value, names: set[str]):
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in names and item not in (None, ""):
                return item
            found = find_value(item, names)
            if found not in (None, ""):
                return found
    elif isinstance(value, list):
        for item in value:
            found = find_value(item, names)
            if found not in (None, ""):
                return found
    return None


def request_json(state: dict, path: str, method: str = "GET", body: dict | None = None, extra_headers: dict | None = None):
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Cookie": cookie_header(state),
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/app",
        "User-Agent": "workbuddy-checkin/1.0",
    }
    if extra_headers:
        headers.update(extra_headers)
    payload = None if body is None else json.dumps(body).encode("utf-8")
    request = Request(f"{BASE_URL}{path}", data=payload, headers=headers, method=method)
    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8", errors="replace")
            status = response.status
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        if exc.code in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
            raise CheckinError("WorkBuddy 登录状态已失效，请重新运行 --setup 并复制新的 state 文件") from exc
        raise CheckinError(f"WorkBuddy API HTTP {exc.code}: {raw[:300]}") from exc
    except URLError as exc:
        raise CheckinError(f"无法连接 WorkBuddy: {exc.reason}") from exc
    if status != HTTPStatus.OK:
        raise CheckinError(f"WorkBuddy API HTTP {status}: {raw[:300]}")
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        if raw.lstrip().startswith("<"):
            raise CheckinError("WorkBuddy 登录状态已失效，请重新运行 --setup 并复制新的 state 文件") from exc
        raise CheckinError(f"WorkBuddy 返回的不是 JSON: {raw[:300]}") from exc
    if isinstance(result, dict) and result.get("code") not in (None, 0, "0", 1001, "1001"):
        message = result.get("msg") or result.get("message") or "API 返回失败"
        raise CheckinError(f"WorkBuddy API 失败 ({result.get('code')}): {message}")
    return result


def account_headers(state: dict) -> dict:
    account = request_json(state, "/console/accounts")
    uid = find_value(account, {"uid", "userid", "user_id", "userid"})
    headers = {}
    if uid is not None:
        headers["X-User-Id"] = str(uid)
    enterprise = find_value(account, {"enterpriseid", "enterprise_id"})
    tenant = find_value(account, {"tenantid", "tenant_id"})
    if enterprise is not None:
        headers["X-Enterprise-Id"] = str(enterprise)
        headers["X-Tenant-Id"] = str(enterprise)
    elif tenant is not None:
        headers["X-Tenant-Id"] = str(tenant)
    return headers


def today_checked_in(status: dict) -> bool:
    found = find_value(status, {"today_checked_in", "todaycheckedin"})
    if isinstance(found, str):
        return found.strip().lower() in {"1", "true", "yes", "y"}
    return bool(found)


def main() -> int:
    try:
        state = load_state()
        headers = account_headers(state)
        status = request_json(state, STATUS_PATH, "POST", {}, headers)
        if today_checked_in(status):
            print("今天已经签到，无需重复操作。")
            return 0
        result = request_json(state, CHECKIN_PATH, "POST", {}, headers)
        if isinstance(result, dict) and result.get("code") in (1001, "1001"):
            print("今天已经签到，无需重复操作。")
            return 0
        verified = request_json(state, STATUS_PATH, "POST", {}, headers)
        if not today_checked_in(verified):
            raise CheckinError(f"签到请求已返回，但复查状态仍未显示成功: {json.dumps(result, ensure_ascii=False)}")
        streak = find_value(verified, {"streak_days", "streakdays"})
        credit = find_value(result, {"credit", "daily_credit", "dailycredit"})
        print(f"签到成功。连续签到: {streak if streak is not None else '-'} 天，今日积分: {credit if credit is not None else '-'}")
        return 0
    except CheckinError as exc:
        print(f"签到失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
