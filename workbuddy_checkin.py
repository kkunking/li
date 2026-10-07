from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = Path(os.environ.get("WORKBUDDY_STATE_PATH", str(ROOT / "playwright_state.json")))
ARTIFACT_DIR = STATE_PATH.parent


def load_config() -> dict:
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"找不到配置文件: {CONFIG_PATH}") from exc
    if not config.get("base_url"):
        raise RuntimeError("config.json 的 base_url 不能为空")
    return config


def save_config(config: dict) -> None:
    CONFIG_PATH.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def first_visible(page, texts: list[str]):
    for text in texts:
        selectors = [
            f"button:has-text('{text}')",
            f"[role=button]:has-text('{text}')",
            f"a:has-text('{text}')",
            f"text={text}",
        ]
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                if locator.is_visible(timeout=800):
                    return locator
            except PlaywrightError:
                continue
    return None


def has_text(page, texts: list[str]) -> bool:
    body = page.locator("body")
    try:
        content = body.inner_text(timeout=3000).lower()
    except PlaywrightError:
        return False
    return any(text.lower() in content for text in texts)


def open_app(playwright, config: dict, headless: bool):
    launch_options = {"headless": headless}
    browser_channel = os.environ.get("WORKBUDDY_BROWSER_CHANNEL")
    if browser_channel:
        launch_options["channel"] = browser_channel
    browser = playwright.chromium.launch(**launch_options)
    context_kwargs = {}
    if STATE_PATH.exists():
        context_kwargs["storage_state"] = str(STATE_PATH)
    context = browser.new_context(**context_kwargs)
    page = context.new_page()
    page.set_default_timeout(config.get("timeout_seconds", 20) * 1000)
    url = config.get("checkin_url") or config["base_url"]
    page.goto(url, wait_until="domcontentloaded")
    return browser, context, page


def setup_session(config: dict):
    with sync_playwright() as playwright:
        browser, context, page = open_app(playwright, config, headless=False)
        print("浏览器已打开。请完成 WorkBuddy 登录，并进入可看到签到按钮的页面。")
        input("完成后回到此终端按回车保存登录状态... ")
        try:
            page.wait_for_load_state("domcontentloaded", timeout=5_000)
        except PlaywrightTimeoutError:
            pass
        if "/login" in page.url.lower():
            browser.close()
            raise RuntimeError("仍在登录页，未保存会话。请完成登录后重新运行 --setup")
        try:
            visible_buttons = [
                text.strip()
                for text in page.locator("button:visible").all_inner_texts()
                if text.strip()
            ]
        except PlaywrightError:
            # WorkBuddy may still be completing a client-side navigation after login.
            visible_buttons = []
        print(f"当前页面: {page.url}")
        print(f"可见按钮: {visible_buttons or '未发现按钮'}")
        if "/login" in page.url.lower():
            browser.close()
            raise RuntimeError("仍在登录页，未保存会话。请在此浏览器窗口完成登录后再按回车")
        if page.url != config.get("checkin_url"):
            config["checkin_url"] = page.url
            save_config(config)
            print(f"已将当前页面写入 config.json: {page.url}")
        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        context.storage_state(path=str(STATE_PATH))
        browser.close()
        print(f"登录状态已保存到 {STATE_PATH}")


def run_checkin(config: dict, headless: bool):
    with sync_playwright() as playwright:
        browser, context, page = open_app(playwright, config, headless=headless)
        try:
            try:
                page.wait_for_load_state("networkidle", timeout=10_000)
            except PlaywrightTimeoutError:
                pass

            if "/login" in page.url.lower():
                print("WorkBuddy 登录状态已失效，请先运行: py workbuddy_checkin.py --setup", file=sys.stderr)
                return 1

            if has_text(page, config.get("success_texts", [])) and not first_visible(page, config["checkin_texts"]):
                print("今天已经签到，无需重复操作。")
                return 0

            button = first_visible(page, config.get("checkin_texts", []))
            if button is None:
                ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
                screenshot = ARTIFACT_DIR / "workbuddy_checkin_error.png"
                page.screenshot(path=str(screenshot), full_page=True)
                print(f"未找到签到按钮。请检查 base_url、页面权限或 checkin_texts。截图: {screenshot}", file=sys.stderr)
                return 2

            button.click()
            deadline = time.time() + config.get("timeout_seconds", 20)
            while time.time() < deadline:
                if has_text(page, config.get("success_texts", [])):
                    print("签到成功。")
                    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
                    context.storage_state(path=str(STATE_PATH))
                    return 0
                time.sleep(0.5)

            ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
            screenshot = ARTIFACT_DIR / "workbuddy_checkin_error.png"
            page.screenshot(path=str(screenshot), full_page=True)
            print(f"点击签到后未检测到成功提示。截图: {screenshot}", file=sys.stderr)
            return 3
        finally:
            browser.close()


def install_task(run_time: str, url: str | None = None, buttons: list[str] | None = None) -> None:
    if os.name != "nt":
        raise RuntimeError("--install-task 只支持 Windows")
    try:
        time.strptime(run_time, "%H:%M")
    except ValueError as exc:
        raise RuntimeError("--time 必须是 HH:MM，例如 09:00") from exc
    task_name = "WorkBuddy自动签到"
    script = str(Path(__file__).resolve())
    task_args = [sys.executable, script, "--headless"]
    if url:
        task_args.extend(["--url", url])
    for button in buttons or []:
        task_args.extend(["--button", button])
    command = [
        "schtasks", "/Create", "/TN", task_name, "/SC", "DAILY", "/ST", run_time,
        "/TR", subprocess.list2cmdline(task_args), "/F",
    ]
    result = subprocess.run(command, capture_output=True, text=True, encoding="mbcs", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "创建任务计划失败")
    print(f"已创建每天 {run_time} 执行的任务: {task_name}")
    

def main() -> int:
    parser = argparse.ArgumentParser(description="WorkBuddy 自动签到器")
    parser.add_argument("--setup", action="store_true", help="打开浏览器完成首次登录并保存会话")
    parser.add_argument("--headless", action="store_true", help="无头模式运行签到")
    parser.add_argument("--install-task", action="store_true", help="创建 Windows 每日任务计划")
    parser.add_argument("--time", default="09:00", help="任务计划执行时间，格式 HH:MM")
    parser.add_argument("--url", help="覆盖 config.json 中的签到地址")
    parser.add_argument("--button", action="append", help="追加签到按钮文案，可重复传入")
    args = parser.parse_args()
    try:
        config = load_config()
        if args.url:
            config["checkin_url"] = args.url
            config["base_url"] = args.url
        if args.button:
            config["checkin_texts"] = args.button + config.get("checkin_texts", [])
        if args.install_task:
            install_task(args.time, url=args.url, buttons=args.button)
            return 0
        if args.setup:
            setup_session(config)
            return 0
        if not STATE_PATH.exists():
            print("尚未保存登录状态，请先运行: py workbuddy_checkin.py --setup", file=sys.stderr)
            return 1
        return run_checkin(config, headless=args.headless)
    except PlaywrightError as exc:
        print(f"Playwright 错误: {exc}", file=sys.stderr)
        return 4
    except Exception as exc:
        print(f"运行失败: {exc}", file=sys.stderr)
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
