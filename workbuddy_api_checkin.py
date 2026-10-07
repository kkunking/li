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


class AccountEndpointError(CheckinError):
    """Authentication or route errors for which another account endpoint may work."""


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
        if exc.code in (301, 302, 303, 307, 308, 401, 403, 404, 405):
            raise AccountEndpointError(f"WorkBuddy API HTTP {exc.code}，请检查登录状态或接口路径") from exc
        raise CheckinError(f"WorkBuddy API HTTP {exc.code}") from exc
    except URLError as exc:
        raise CheckinError(f"无法连接 WorkBuddy: {exc.reason}") from exc
    if status != HTTPStatus.OK:
        raise CheckinError(f"WorkBuddy API HTTP {status}")
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        if raw.lstrip().startswith("<"):
            raise AccountEndpointError("WorkBuddy 未返回账户 JSON，请重新运行 --setup 并复制新的 state 文件") from exc
        raise CheckinError("WorkBuddy 返回的不是 JSON") from exc
    allowed_codes = (0, "0", 1001, "1001") if path == CHECKIN_PATH else (0, "0")
    if not isinstance(result, dict) or result.get("code") not in allowed_codes:
        raise CheckinError("WorkBuddy API 返回业务失败或缺少成功状态码")
    return result


def account_headers_from_response(result: dict, state: dict) -> dict:
    if not isinstance(result, dict) or result.get("code") not in (0, "0"):
        raise CheckinError("账户接口未返回成功状态")
    data = result.get("data")
    if not isinstance(data, dict):
        raise CheckinError("账户接口缺少 data")
    accounts = data.get("accounts") if "accounts" in data else [data]
    if not isinstance(accounts, list) or not accounts:
        raise CheckinError("账户接口没有可用账户")
    if any(not isinstance(account, dict) or not isinstance(account.get("uid"), str)
           or not account["uid"].strip() for account in accounts):
        raise CheckinError("账户接口含无效账户或缺少 uid")
    selected_ids = {
        item.get("value")
        for origin in state.get("origins", [])
        if origin.get("origin", "").rstrip("/") == BASE_URL
        for item in origin.get("localStorage", [])
        if item.get("name") == "CODEBUDDY_IDE_SELECTED_ACCOUNT_ID" and item.get("value")
    }
    if len(selected_ids) > 1:
        raise CheckinError("保存的账户选择存在歧义，请重新登录并选择账户")
    selected = next(iter(selected_ids), None)
    matches = [a for a in accounts if selected == (
        a["uid"] if a.get("type", "personal") == "personal" else a.get("enterpriseId"))] if selected else []
    if len(matches) == 1:
        account = matches[0]
    elif selected and not matches and len(accounts) > 1:
        raise CheckinError("保存的账户选择已不可用，请重新选择账户")
    elif len(matches) > 1:
        raise CheckinError("账户选择存在歧义，请重新选择账户")
    elif len(accounts) == 1:
        account = accounts[0]
    else:
        personal = [a for a in accounts if a.get("type", "personal") == "personal"]
        if len(personal) != 1:
            raise CheckinError("存在多个账户，请先在网页选择签到账户后重新导出登录状态")
        account = personal[0]
    headers = {"X-User-Id": account["uid"]}
    enterprise = account.get("enterpriseId")
    if account.get("type", "personal") != "personal" and not enterprise:
        raise CheckinError("企业账户缺少 enterpriseId")
    if enterprise:
        headers["X-Enterprise-Id"] = str(enterprise)
        headers["X-Tenant-Id"] = str(enterprise)
    return headers


def account_headers(state: dict) -> dict:
    for index, path in enumerate(("/console/account", "/console/accounts")):
        try:
            result = request_json(state, path)
        except AccountEndpointError:
            if index == 0:
                continue
            raise
        return account_headers_from_response(result, state)
    raise CheckinError("无法获取账户")


def today_checked_in(status: dict) -> bool:
    found = find_value(status, {"today_checked_in", "todaycheckedin"})
    if found is None:
        raise CheckinError("签到状态缺少 today_checked_in，已停止，未继续提交签到")
    if isinstance(found, str):
        normalized = found.strip().lower()
        if normalized in {"1", "true", "yes", "y"}:
            return True
        if normalized in {"0", "false", "no", "n"}:
            return False
    elif isinstance(found, bool) or isinstance(found, int) and found in (0, 1):
        return bool(found)
    raise CheckinError("签到状态 today_checked_in 类型无效，已停止")


def main() -> int:
    try:
        state = load_state()
        headers = account_headers(state)
        status = request_json(state, STATUS_PATH, "POST", {}, headers)
        if today_checked_in(status):
            print("今天已经签到，无需重复操作。")
            return 0
        result = request_json(state, CHECKIN_PATH, "POST", {}, headers)
        verified = request_json(state, STATUS_PATH, "POST", {}, headers)
        if not today_checked_in(verified):
            raise CheckinError("签到请求已返回，但复查状态仍未显示成功")
        if result.get("code") in (1001, "1001"):
            print("已复查确认今天已经签到。")
            return 0
        streak = find_value(verified, {"streak_days", "streakdays"})
        credit = find_value(result, {"credit", "daily_credit", "dailycredit"})
        print(f"签到成功。连续签到: {streak if streak is not None else '-'} 天，今日积分: {credit if credit is not None else '-'}")
        return 0
    except CheckinError as exc:
        print(f"签到失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
