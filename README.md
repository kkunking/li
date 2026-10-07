# WorkBuddy 自动签到器

使用 GitHub Actions 每天自动执行 WorkBuddy 签到，不需要自己的服务器或保持电脑开机。Actions 运行时通过 Cookie 调用 WorkBuddy 接口，不启动浏览器。

## 配置 GitHub Actions

### 1. 在电脑上登录并导出会话

安装依赖后运行首次登录：

```powershell
py -m pip install -r requirements.txt
py -m playwright install chromium
py workbuddy_checkin.py --setup
```

Windows 可以直接双击 `login-workbuddy.cmd`，它会用 Edge 打开登录窗口。浏览器打开后登录 WorkBuddy；程序自动验证账户并保存，无需寻找签到按钮或回终端按回车。最多等待 10 分钟，成功后项目目录会生成 `playwright_state.json`。它包含登录 Cookie，等同于登录凭证，不能提交到仓库、发送给他人或粘贴到 issue。

### 2. 建立 GitHub 仓库并上传代码

在 GitHub 新建一个空仓库。建议设为 Private，然后在项目目录运行以下命令，把 `USERNAME` 和 `REPOSITORY` 换成你的 GitHub 用户名和仓库名：

```powershell
git init
git add .
git commit -m "Add WorkBuddy check-in workflow"
git branch -M main
git remote add origin https://github.com/USERNAME/REPOSITORY.git
git push -u origin main
```

`.gitignore` 会排除 `playwright_state.json` 和 `data/`。推送前可用 `git status --short` 检查待提交文件中没有登录状态文件。

### 3. 把登录状态保存为 GitHub Secret

在仓库打开 **Settings → Secrets and variables → Actions → New repository secret**：

- Name：`WORKBUDDY_STATE_JSON`
- Secret：`playwright_state.json` 的全部内容，从 `{` 到 `}`，不要再加引号

工作流只会把这个 Secret 临时写入运行器的 `data/playwright_state.json`，不会把它打印到日志或提交回仓库。

### 4. 手动测试并启用定时运行

打开仓库的 **Actions → WorkBuddy 自动签到 → Run workflow**，手动执行一次并查看运行日志。工作流也会每天北京时间 **09:00** 自动运行（GitHub cron 使用 UTC，所以设为 `01:00`）。GitHub 的定时启动可能延迟几分钟，且只在默认分支上运行；请确认仓库已启用 Actions。

GitHub Actions 对小型个人任务通常够用；私有仓库会使用账户的 Actions 分钟额度，可在 GitHub 计费页面查看剩余额度。

### 5. 会话过期时更新

网页登录 Cookie 可能过期。若日志显示登录状态失效，在电脑上重新执行 `py workbuddy_checkin.py --setup`，然后编辑 `WORKBUDDY_STATE_JSON` Secret 并替换成新 `playwright_state.json` 的全部内容，再手动运行工作流验证。

## 本机手动运行

登录状态导出后，可以在电脑上直接运行 API 签到脚本：

```powershell
py workbuddy_api_checkin.py
```

首次登录默认使用 Playwright Chromium。Windows 如果下载 Chromium 较慢，也可以复用已安装的 Edge：`$env:WORKBUDDY_BROWSER_CHANNEL = "msedge"` 后再运行 `py workbuddy_checkin.py --setup`。

`workbuddy_checkin.py` 是基于 Playwright 的本机浏览器方式，适合首次登录和导出会话；API 客户端会先检查今天是否已签到，执行后再复查状态。重复签到会直接跳过。登录失效或接口失败时，脚本以退出码 `1` 结束。

目前 API 客户端使用 WorkBuddy 网页版的 `/billing/meter/checkin-activity-status` 和 `/billing/meter/daily-checkin` 接口。站点接口变化、账号权限或组织设置不同，都可能导致签到失败；请通过 Actions 运行日志查看结果。尚未使用你的账号验证真实签到。
