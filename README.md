# 雷神加速器 · 挂机自动暂停

[![CI](https://github.com/lyxcll/leigod-autopause/actions/workflows/ci.yml/badge.svg)](https://github.com/lyxcll/leigod-autopause/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg)](leigod_autopause.py)
[![exe](https://img.shields.io/badge/download-.exe%208.9%20MB-6D5BD0.svg)](https://github.com/lyxcll/leigod-autopause/releases/latest/download/LeigodAutoPause.exe)

挂机 / 出门没关电脑时，自动把**雷神加速器**的计时暂停掉，避免白烧时长。

纯本地小工具：单文件 Python，**只用标准库**，不需要装依赖、不需要抓包、不需要手动填 token。

两种触发方式，随便配一个或都开：

```
① 游戏退出即暂停    正在加速的游戏一关，90 秒后立刻停掉计时（默认）
② 挂机自动暂停      加速中 + 键鼠空闲 ≥ 15 分钟 + 本轮已加速 ≥ 3 分钟
        ↓
自动调用官方 pause 接口，把计时停掉
```

**不用按游戏配置**：雷神客户端每次启动游戏都会在主日志里写下这条游戏的进程名
（`ipc handle launch-local-game with [{"game_id":110,"game_process":"RainbowSix.exe,RainbowSix_Vulkan.exe"}]`），
工具直接读它，所以加速文明6、彩六、还是别的什么，关掉就停。下次开游戏、客户端点「恢复」即继续计时。

---

## 为什么不是现成的 GitHub Action

GitHub 上已有的几个雷神自动暂停项目都是「云端定时暂停」：

| 项目 | 语言 | 做法 | 局限 |
| --- | --- | --- | --- |
| [hobk/leishen-auto](https://github.com/hobk/leishen-auto) | Go | GitHub Actions 每天 1:00 调一次 pause | 一天只停一次，跟你在不在电脑前无关 |
| [JasonYANG170/GameTimePause](https://github.com/JasonYANG170/GameTimePause) | Python | Actions 每天 3:00 登录后暂停 | 同上，还要把账号密码放 Secrets |
| [himcs/LeishenAuto](https://github.com/himcs/LeishenAuto) | JS | 本地/云端定时 | 2024 年停更 |

它们解决的是「半夜忘了关加速器」，解决不了「我下午挂机去吃饭了」。
所以这个工具做成**本地常驻守护**：盯住客户端的加速状态和你真实的键鼠空闲时间，该停的时候立刻停。

> 如果你确实想要云端兜底，仓库里的 [`.github/workflows/optional-daily-pause.yml`](.github/workflows/optional-daily-pause.yml) 是个可选的每日暂停工作流，用法见文末。

---

## 原理

1. **拿 token**：雷神客户端会把请求 URL 写进自己的 HTTP 日志（`resources\llogs\http-*.log`），里面就有 `account_token=...`。工具自动读取，缓存在 `.token_cache`，**不需要你 F12 抓包**。
2. **读加速状态**：主日志里每个加速状态变化都会打一条
   `notify [accelerating-status-change] {"id":"271","status":"running","option":{"game_id":271,"game_name":"文明6"}}`。
   `status=running` 表示正在加速；`normal` 表示已停。工具解析最近两个日志文件，还原「当前是否加速、什么游戏、这一轮从什么时候开始」。
3. **知道是哪个游戏**：主日志里还有 `ipc handle launch-local-game with [{"game_id":110,"game_process":"RainbowSix.exe,RainbowSix_Vulkan.exe"}]`，
   工具用最近 7 天日志里的这类事件建一张 `game_id → 进程名` 表（按文件 mtime 缓存，不会每轮重读），
   于是「当前加速的是哪个游戏、它该看哪些进程」全自动，换游戏不用改配置。
4. **读空闲时长**：Windows `GetLastInputInfo`，即真实的键鼠空闲时间（不是工具自己猜的）。
5. **暂停**：`POST https://webapi.leigod.com/api/user/pause`，body `{"account_token": "...", "lang": "zh_CN"}`。
   - `code=0` → 暂停成功
   - `code=400803` → 本来就已暂停（算成功）
   - `code=400007` → 登录态过期，去客户端重新登录

几个实测出来的坑，工具里都处理了：

- 日志文件里 UTF-8 和 GBK 混着写，部分 `game_name` 是坏字符（`����6`），遇到就退回显示 `游戏#271`；
- 客户端暂停后自己仍可能一直记着 `running`，所以不能靠客户端界面判断，得调接口看返回码；
- 切换游戏 / 重连会连续冒 `normal → before-start → running`，间隔 10 分钟内视为同一轮加速，不会把「持续的挂机」误判成「刚开始」。

---

## 用法

### 最省事：下载 exe，双击就用（免 Python）

[`LeigodAutoPause.exe`](https://github.com/lyxcll/leigod-autopause/releases/latest/download/LeigodAutoPause.exe) ← 点这里下载（8.7 MB，单文件，**不用装 Python**）

1. 丢进任意目录，比如 `D:\Tools\leigod-autopause\`（`config.json`、`autopause.log` 都生成在这个目录里；目录不可写时自动改用 `%LOCALAPPDATA%\leigod-autopause`）；
2. **双击**它，出现菜单（顶部实时显示现在加不加速、守护在不在）：

   ```
   ==========================================================
     雷神加速器 · 挂机自动暂停   v1.2.0
     目录 D:\Tools\leigod-autopause
     状态 加速中 · 守护中 · D:\LeiGod_Acc
   ==========================================================
     1) 安装 / 修复自启，并在后台守护   （推荐）
     2) 刷新当前状态（只读，不会暂停）
     3) 立即暂停一次（测试接口）
     4) 停止守护并取消开机自启
     5) 看日志（最近 20 行）
     6) 打开配置文件 config.json
     7) 打开程序目录
     0) 退出

   请选择 [1-7,0]（直接回车 = 刷新状态）:
   ```

3. 按 `1` 回车。它会注册开机自启、立刻在后台跑起来，然后告诉你去看 `autopause.log`。
   之后再双击，菜单会显示「守护中」；想让它自己看日志就按 `5`，不用再开资源管理器。

> Windows 第一次运行会提示「已保护你的电脑」：点 **更多信息 → 仍要运行** 即可。
> 这个 exe 没有代码签名（个人项目），但代码全部开源，就在本仓库的 `leigod_autopause.py` 里。

不需要 GitHub 账号，不需要填 token —— 它读**你自己电脑上**雷神客户端的日志拿登录态。

### 或者：只下 Python 单文件

[`leigod_autopause.py`](https://github.com/lyxcll/leigod-autopause/releases/latest/download/leigod_autopause.py) ← 点这里下载（31 KB，单文件，无依赖，需要本机有 Python 3.9+）

放进任意目录（比如 `D:\Tools\leigod-autopause\`），然后：

```powershell
cd D:\Tools\leigod-autopause
python leigod_autopause.py status     # 1. 只读，先确认它认识你的客户端
python leigod_autopause.py install    # 2. 注册开机自启，后台守护
```

不需要 GitHub 账号，不需要填 token —— 它读**你自己电脑上**雷神客户端的日志拿登录态。

### 或者 clone 仓库

```powershell
git clone https://github.com/lyxcll/leigod-autopause.git
cd leigod-autopause
python leigod_autopause.py status
python leigod_autopause.py install
```

### 全部命令

环境：Windows + Python 3.9+（`python` 在 PATH 里即可）。

> 用 exe 版的话，把下面的 `python leigod_autopause.py` 换成 `LeigodAutoPause.exe` 就行，
> 比如 `LeigodAutoPause.exe status`、`LeigodAutoPause.exe selftest`。

```powershell
# 1. 看一眼现在什么状态、会不会触发（只读，不会暂停）
python leigod_autopause.py status

# 2. 前台常驻守护（Ctrl+C 退出）
python leigod_autopause.py watch

# 3. 先演练，不真的调接口
python leigod_autopause.py watch --dry-run

# 4. 立刻暂停一次（测试用）
python leigod_autopause.py pause

# 5. 注册开机自启 / 取消
python leigod_autopause.py install
python leigod_autopause.py uninstall

# 6. 离线自检（不碰客户端、不发请求，改完代码先跑这个）
python leigod_autopause.py selftest
```

`status` 输出长这样：

```
======================================================
程序目录 : D:\Tools\leigod-autopause
安装目录 : D:\LeiGod_Acc
客户端   : 运行中
守护进程 : 运行中（后台自动守护）
加速状态 : 加速中 (log status=running)
当前游戏 : 文明6  已加速 42.3 分钟
游戏进程 : 在跑 CivilizationVI.exe（自动识别）
键鼠空闲 : 18.5 分钟（阈值 15 分钟）
是否触发 : 会暂停 —— 加速中 + 长时间没操作
======================================================
```

「守护进程」那行直接告诉你自启是不是活的：`未运行` 就双击 exe 按 `1` 修一下。

### 配置 `config.json`

首次运行自动生成在同目录（当前默认配置已按文明6 调好）：

```json
{
  "idle_minutes": 15,
  "min_accel_minutes": 3,
  "check_interval_seconds": 30,
  "only_when_client_running": true,
  "game_exe_names": [],
  "auto_detect_game_process": true,
  "pause_on_game_exit": true,
  "game_exit_grace_seconds": 90,
  "game_absent_pause_seconds": 300,
  "extra_install_dirs": []
}
```

| 字段 | 说明 |
| --- | --- |
| `idle_minutes` | 键鼠空闲多少分钟才允许暂停，默认 15 |
| `min_accel_minutes` | 刚开加速的前几分钟不动，防止「刚点开就被停」 |
| `check_interval_seconds` | 守护轮询间隔（最小 10） |
| `only_when_client_running` | 客户端都没开就别折腾 |
| `auto_detect_game_process` | **自动识别**当前加速游戏的进程名（从雷神日志的 `launch-local-game` 事件里读），默认开 |
| `game_exe_names` | 手动指定进程，留空即自动识别；填了则优先用手填的（自动识别失败时的兜底） |
| `pause_on_game_exit` | **游戏退出即暂停**。默认开启；关掉就只剩挂机空闲判断 |
| `game_exit_grace_seconds` | 游戏退出后宽限几秒才停，默认 90。给「退出重进 / 崩溃重启 / 退出来查攻略」留余量 |
| `game_absent_pause_seconds` | 加速中但一直没见游戏在跑，多久后也停，默认 300。用于「先关游戏、后忘了关加速」或工具重启后没看到游戏进程的场景；填 `0` 关闭这条 |
| `extra_install_dirs` | 雷神装在非常规目录时手动补，例如 `["E:\\LeiGod_Acc"]`；配了会优先使用 |
| `heartbeat_minutes` | 日志里多久打一行 `💓 心跳`（默认 30，填 `0` 关闭）；用来确认守护还活着 |

改完配置**不用重启**（守护每轮都重新读），但改 `config.json` 的编码请保持 UTF-8。

升级后配置文件会自动补齐新字段（日志里写一行「🔧 配置已升级，补上 N 个新项」），
手滑填了离谱的值也会被拉回安全范围（比如 `idle_minutes: -5` → `0`、`check_interval_seconds: 1` → `10`）。

### 不用按游戏配置

进程名是雷神客户端自己记在日志里的（`game_process` 字段），工具自动跟着当前加速的游戏走：

| 场景 | 行为 |
| --- | --- |
| 游戏在跑、你挂机 15 分钟 | **不暂停**（游戏进程还在） |
| 关掉正在加速的游戏（文明6、彩六……任何游戏） | 90 秒后**立刻暂停**计时，不用等空闲 15 分钟 |
| 关了又马上重开 | 90 秒宽限期内重开，不会被误停 |
| 中途换了游戏 | 自动切到新游戏的进程继续盯 |
| 还没开游戏就先点了加速 | 5 分钟后仍没见游戏进程 → 暂停（`game_absent_pause_seconds`） |
| 看剧 / 挂下载，游戏没开 | 回到「空闲 15 分钟」规则 |

### 开机自启

`install` 会先尝试建计划任务 `LeigodAutoPause`（登录时启动，无窗口）。
**普通权限下 `schtasks` 可能报 `Access is denied`**，此时自动回退为启动文件夹里的
`LeigodAutoPause.vbs`（`shell:startup`），效果一样：登录后静默拉起 `pythonw.exe`。

想用计划任务的话，用管理员身份重跑一次 `install` 即可。

启动文件夹里那个 `LeigodAutoPause.vbs` 不是一次性运行，而是个**守护循环**：

```vbs
Do
  sh.Run "pythonw.exe ... watch", 0, True   ' 隐藏窗口运行，等它退出
  WScript.Sleep 5000                       ' 退出就 5 秒后重来
Loop
```

所以 `pythonw` 被结束、崩溃、甚至你手动杀了，5 秒后都会自动回来；配合命名互斥量，
重复启动不会跑出两个实例（第二个会自己退出，日志里写「已经有一个守护在跑了」）。

---

## 故障排查：怎么确认它活着

```powershell
# 1) 守护进程在不在（应该有一个 pythonw.exe）
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" | Select ProcessId,CreationDate

# 2) 最近发生了什么（每 30 分钟有一行 💓 心跳，启动时有一行 📊 快照）
Get-Content E:\DSH4\leigod-autopause\autopause.log -Tail 8

# 3) 它现在"看到"了什么（会不会触发）
cd E:\DSH4\leigod-autopause
python leigod_autopause.py status

# 4) 兜底：手动跑一次自启脚本（VBS 会一直守着，Ctrl+C 结束不了就关窗口）
wscript "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\LeigodAutoPause.vbs"
```

日志里正常长这样：

```
[22:19:19] 👀 开始守护 v1.2.0（pid 27900）：D:\LeiGod_Acc，每 30s 检查一次（空闲阈值 15 分钟，游戏退出即暂停）
[22:19:20] 📊 当前：加速中（彩虹六号:围攻 X），游戏进程 无（自动识别）
[22:19:22] ✅ 已暂停（加速中但游戏没在跑，无需再加速（彩虹六号:围攻 X））msg=成功
```

如果是「加速中、游戏进程 在跑」却一直不停 —— 那说明游戏真没退干净（比如 R6 的
`RainbowSix_BE.exe`、育碧后台还留着），这是预期行为；把 `pause_on_game_exit` 关掉
或把 `game_exit_grace_seconds` 调小再观察。

---

## 常见问题

**暂停了但客户端界面还显示在加速？**
正常。客户端缓存状态、每 10 分钟才轮询一次；以接口返回码为准。点「恢复」就会继续计时。

**会不会重复暂停、重复扣时长？**
不会。同一轮加速只尝试一次（按这轮加速的起始时间去重），返回 `400803` 也当成功。
连续失败 5 次会放弃本轮，避免刷日志。

**token 什么时候失效？**
你在客户端退出登录 / 换号后失效（返回 `400007`）。工具每轮都重新读日志拿最新 token，所以重新登录后会自动跟上。

**杀毒软件 / SmartScreen 报警？**
没有网络服务、没有注入，只是个读日志 + 发一个 HTTP 请求的小工具。exe 是 PyInstaller 打的、没签名，
所以下载后第一次运行会出现「已保护你的电脑」——点 **更多信息 → 仍要运行**。
不放心的可以直接用 Python 版 `leigod_autopause.py`（内容完全开源），或者先 `--dry-run` 跑几天。

**日志文件在哪？**
`autopause.log`（同目录，自动滚动，只留最近 800 行），超过 512 KB 自动截断。菜单按 `5` 直接看最近 20 行。

**把 exe 放进 `C:\Program Files` 后生成不了配置？**
那个目录普通权限写不了。工具检测到目录不可写会自动退到 `%LOCALAPPDATA%\leigod-autopause\`，
菜单/`status` 的「程序目录」那行会标出来。想固定住就把 exe 放到你自己有写权限的目录（比如 `D:\Tools\...`）。

**程序自己崩了怎么办？**
崩溃会写进 `autopause.log`：一行 `💥 崩溃：…` 加完整 traceback（不会弹窗、不会静默消失）。
同时 VBS 守护循环会在 5 秒后重新拉起，所以「崩一次」通常只丢一次检查。

**怎么看版本？是不是新版？**
`LeigodAutoPause.exe --version`，或者双击看菜单表头（`v1.2.0`）。`--help` 列出全部命令。

**之前挂机几个小时都没停？**
先看日志里有没有 `💓 心跳`。没有 = 进程当时不在（被杀 / 没开机自启），工具再聪明也没用；
现在 VBS 循环会让它 5 秒内自愈，日志里也能看到重启记录。

---

## 可选：云端每日兜底（GitHub Actions）

仓库里 `.github/workflows/optional-daily-pause.yml` 是个每天北京时间 1:00 暂停一次的工作流，
适合「电脑关机了也不想让加速器白跑」的情况。开启方式：

1. Settings → Secrets and variables → Actions → New repository secret，名字 `LEIGOD_TOKEN`，值填你的 `account_token`；
2. Actions 里手动 `Run workflow` 试一次；
3. 不需要就在 Settings → Actions 里禁用这个 workflow。

`account_token` 自己怎么拿：打开雷神客户端，随便点一下（触发网络请求），然后到
`<安装目录>\resources\llogs\http-YYYY-MM-DD.log` 里搜 `account_token=`。

⚠️ token 等同于账号的加速凭证，**别提交进仓库、别贴到公开 issue**，只放 Secrets。

---

## 维护 / 适配新版本

更新记录见 [CHANGELOG.md](CHANGELOG.md)。

这个工具只依赖两样东西：**客户端日志格式**和**pause 接口的返回码**。雷神升级后可以这样自查：

| 现象 | 先看哪里 | 怎么修 |
| --- | --- | --- |
| `status` 一直「未加速」 | `main-*.log` 里还有没有 `notify [accelerating-status-change]` | 改了字段名就改 `EVENT_RE` |
| 别人的游戏识别不到进程 | `main-*.log` 里还有没有 `launch-local-game with` | 改了事件名就改 `LAUNCH_RE`；实在没有就在 `config.json` 手动填 `game_exe_names` |
| 暂停返回 `code=400007` | token 过期 | 客户端重新登录，工具下一轮自动读新 token |
| 暂停返回别的 code | 接口改了 | 拿返回码开 issue，通常改 `call_pause` 的 `code` 判断 |
| 一切正常但就是不触发 | `autopause.log` 有没有 `💓 心跳` | 没有心跳 = 进程没了；有心跳 = 看 `📊` 那行它看到了什么 |

CI（`.github/workflows/ci.yml`）在每次 push/PR 时跑 `selftest`：日志解析、进程名识别、token 提取、配置读取 —— 改这几块时它会兜住。

exe 由 `build_exe.py`（PyInstaller）打出，`build-exe.yml` 在打 tag 时自动构建并附到 Release，你也可以本地 `python build_exe.py` 复现。

**PR 请自带一个 selftest 断言**（改哪个解析器就在 `cmd_selftest` 里补一个 `check`），这是这个仓库唯一的硬性要求。

---

## 免责声明

仅操作你自己账号的暂停接口，不涉及破解、不绕过任何付费限制。接口是客户端自己在用的，
如果哪天官方改了路径或参数，工具需要跟着改 —— 这也是它保持单文件、好改的原因。

MIT License.
