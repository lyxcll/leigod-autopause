# 安全说明

这份文档把「我们防住了什么、没防住什么」写清楚，不粉饰。发现文档和代码不一致，请开 issue。

## 一句话威胁模型

- **信任**：你机器上的雷神客户端日志、`api.leigod.com`、本仓库源码与 CI 产物。
- **部分不信任**：下载渠道（所以要校验，但校验有上限，见下文）。
- **无法防**：已经以你的用户身份运行的恶意程序、管理员/SYSTEM 级对手、内存取证。

## 1. `account_token` 等同于账号凭证

工具从雷神客户端日志里读 `account_token` 调官方 pause 接口。拿到它的人不需要密码
就能暂停/恢复你的加速，所以它按凭证对待。

**已经做的加固（v1.2.3 起）**

| 措施 | 位置 | 效果 |
| --- | --- | --- |
| Windows DPAPI 加密缓存 | `.token_cache`（`save_token` / `load_cached_token`） | 只有当前 Windows 账户能解开；文件被拷到别的账户/机器 = 一堆密文 |
| 收紧文件 ACL | `harden_file()`：`icacls /inheritance:r` 只留 当前用户 + SYSTEM + Administrators | 去掉系统默认继承的 `Authenticated Users:(M)` / `Users:(RX)`（同机其他账户原本可读走 token） |
| 日志脱敏 | `redact()`，`log()` 写盘前统一处理 | `account_token=…`、JSON 形式、`github_pat_…` 一律变 `***`；异常/响应体里的凭证也不会落盘 |
| 终端只显示前 4 位 | `mask()` → `2a3G*** (len=27)` | `status` 输出可以直接贴到 issue |

**仍然挡不住的（明确列出）**

- **同账户的恶意程序**：DPAPI 是「按用户」加密，同一用户下的程序可以调用同样的 API 解密；
  它本来也能直接读雷神自己的日志。用户态加密防的是「别的账户/拷走文件」，不是防你中的毒。
- **管理员 / SYSTEM**：可以读任何文件、改 ACL、注入进程。无解。
- **雷神客户端把 token 明文写在日志里**：这是它的设计，本工具只能尽量不扩散、不外传。
- **token 有效期**：退出雷神登录后旧 token 是否立即失效，取决于雷神服务端，未验证。

建议：不要把 `.token_cache`、`autopause.log`、`config.json` 传给别人或提交进仓库
（`.gitignore` 已覆盖）。贴 issue 时 `status` 输出是脱敏的，可以直接贴。

## 2. exe 没有代码签名，意味着什么

- 没有 Authenticode 签名 → Windows 无法证明「这是谁发布的」，SmartScreen 会弹拦截，**这是正常现象**；
- `checksum` / `SHA256SUMS.txt` 能证明的只有：**你下到的字节 = Release 页记录的那份**。
  它能发现下载截断、CDN 传坏、镜像投毒；
- **它挡不住**：能改 Release 的攻击者（exe 和 `SHA256SUMS.txt` 同源，一起换就一致）、
  上游被入侵后重新打包、以及「这个 exe 到底是不是这份源码构建的」；
- 所以：`checksum` **不是**防篡改，也**不是**身份认证。`--version` 里的提示文字已按此措辞。

**相对可信的部分**

- 源码公开、可审计，CI 用 `build-exe.yml` 从源码构建并上传产物；
- 但 PyInstaller 产物**不是 bit-for-bit 可复现**（含构建时间戳/环境差异），
  所以「下载的 exe 就是这份源码」只能靠信任 GitHub Actions 的构建记录，而不能本地复算。
- 想完全自主：`pip install pyinstaller pillow && python build_exe.py` 自己构建，只信自己的产物。

**真签名怎么办**：买 OV/EV 证书或申请 [SignPath](https://signpath.io/) 开源签名，
把 `.pfx` 放进 secrets（`CODE_SIGN_PFX_BASE64` / `CODE_SIGN_PFX_PASSWORD`）即可，
流水线已内置 `scripts/sign_exe.ps1`，**顺序是「先签名、后算 SHA256」**。

## 3. 本地攻击面清单

- 程序只做三件事：读雷神日志、调 `tasklist`/`icacls`/`schtasks`、`POST https://webapi.leigod.com/api/user/pause`；
- 没有监听端口、没有反连、不执行日志里的内容（日志只用于正则提取）；
- 自启：优先计划任务（需管理员，普通用户会 `Access is denied` 后回退），否则写启动文件夹的
  VBS 循环守护；VBS 只运行写死的 exe 路径；
- 供应链：`.github/workflows/*.yml` 里的 actions 已按 **commit SHA** 固定（注释里标了版本），
  避免 tag 被移动后执行到别的代码；
- `optional-daily-pause.yml` 只打印响应状态码，不打印 token；token 来自 Repository secret；
- `uninstall` 会删计划任务和 VBS、结束守护进程；`config.json` / 日志 / `.token_cache` 保留，需手动删。

## 5. 第二轮自审记录（v1.2.4）

用户要求「不能藏漏洞，不能藏后门」，所以把审计过程和结果都摊开写。**没能证实的地方也照实写。**

### 审了什么

| 范围 | 方法 | 结果 |
| --- | --- | --- |
| 网络出口 | 全仓库搜 `urllib` / `http` / socket / 域名 | 只有 `https://webapi.leigod.com/api/user/pause` 一处（`PAUSE_URL`），无其它回连、无遥测 |
| 动态执行 | 搜 `eval` / `exec` / `os.system` / `shell=True` / `EncodedCommand` / `-c` 拼接 | 0 处；日志内容只走正则/JSON 解析，从不拼命令 |
| 子进程 | 逐个看 `subprocess` 调用点 | 全部 argv 列表形式，`icacls` / `reg query` / `tasklist` / `schtasks` / `taskkill` 参数里没有日志内容 |
| 打包产物 | 自写 `scan_exe.py` 扫本地产物**与 Release 产物** | 字符串里只有 `webapi.leigod.com`、PyInstaller manifest 的 `schemas.microsoft.com`；无 `eval(`/`exec(`/`VirtualAlloc`/`WriteProcessMemory`；`LoadLibrary` 1 次（ctypes）、`base64` 2 次（stdlib 内部） |
| 打包内容 | `PyInstaller archive_viewer -l` | 只有 `leigod_autopause` + stdlib 模块与 `api-ms-win-*`/`_ssl.pyd`，无第三方库 |
| git 历史 | 扫所有历史 blob：`github_pat_`、`PRIVATE KEY`、`account_token=`、大文件 | 只命中 selftest 里的假串 `account_token=abcDEF123456_xyz`；无真实凭证、无 1MB+ 大文件 |
| `.git/config` | 直接读 | 只有 `url`、`http.proxy=http://127.0.0.1:7897`、`user.name/email`，**没有**写入任何 token |
| 本机自启 | `schtasks /Query`、启动文件夹、HKCU/HKLM Run | 没有计划任务（普通用户建不了）；只有启动文件夹的 `LeigodAutoPause.vbs`（243 B，只运行写死的 exe 路径）；Run 键里没有本工具 |
| 运行进程 | `Win32_Process` + `Get-NetTCPConnection` | 只有 `LeigodAutoPause.exe watch`；空闲时无 TCP 连接（只在触发暂停时发一次 HTTPS） |

### 这一轮修掉的问题

1. `test_game_exit.py` 原来 `taskkill /F /IM pythonw.exe`（**误杀所有 Python GUI 程序**）、
   以文本读 DPAPI 密文缓存（必然 `UnicodeDecodeError`）、把**真实 token 明文**写进 `%TEMP%` 且异常不清理。
   → 改为独立临时目录 + DPAPI 密文 + `try/finally` 清理，`LEIGOD_AUTOPAUSE_MUTEX` 隔离单实例。
2. `sign_exe.ps1` 的临时 `.pfx` 只在成功路径删除，中途抛异常会**把私钥留在 %TEMP%**；
   且可再导出。→ `try/finally` 必删 + `EphemeralKeySet` + 临时文件 ACL 收紧。
3. `call_pause` 用默认 `urlopen`，会跟随 3xx（POST 体里的 token 可能被带去别的主机）。→ 禁用重定向 + 固定主机核对 + TLS 1.2+。
4. DPAPI 失败时静默退化明文、`icacls` 失败静默 `pass`。→ 都改为日志警告（同一进程只警告一次）。
5. CI 里 `pip install pyinstaller pillow` 是浮动版本。→ 固定 `6.22.3` / `13.0.0`。

### 用 API 没能证实的（token 缺 Administration 权限，全部 403）

- 仓库 webhooks（`/hooks`）、Deploy keys（`/keys`）、
  Actions 默认权限（`/actions/permissions`）、Actions secrets（`/actions/secrets`）、分支保护细节。
  能确认的只有：公开仓库、协作者只有 `lyxcll` 一人、environments 为 0。
- **已确认的事实**：`GET /repos/lyxcll/leigod-autopause/branches/main` 返回 `protected: false`
  —— 也就是说 `main` **目前没有分支保护**（可被 force push / 删除）。建议在网页上开（见下）。

请在网页 **Settings** 里自查这几项（一次性，2 分钟）：

1. **Branches** → 是否给 `main` 开了保护（至少「禁止 force push / 删除」）；
2. **Webhooks** → 应该为空（本仓库不需要任何 webhook）；
3. **Deploy keys** → 应该为空（推送用的是临时 header，没有常驻密钥）；
4. **Actions → General** → Workflow permissions 是否已设为 **Read repository contents**（最小权限）；
5. **Secrets and variables → Actions** → 应该只有可选的 `CODE_SIGN_*`（不签名就不需要）。

### 仍然存在、也修不掉的（说清楚）

- **程序目录可被同机其他账户改写**：`E:\Edge` 这类目录默认继承 `Authenticated Users:(M)`，
  别人能把 exe 换掉。→ 建议放到只有你能写的目录：`%LOCALAPPDATA%\LeigodAutoPause\`
  或 `C:\Program Files\...`（程序本身不要求管理员，放哪都行）。
- **同账户恶意程序**：能读雷神日志、能调 DPAPI、能改 exe。用户态无解。
- **雷神客户端日志明文带 `account_token`**：这是它的设计，本工具只能不外传、不落自己的盘。
- **没有签名、构建不可复现**：只能靠「源码 + CI 构建记录 + 自己构建」建立信任。
- **PowerShell 5.1 的编码坑**：`scripts/sign_exe.ps1` 必须带 UTF-8 BOM，否则 5.1 按 ANSI 读会语法错误
  （CI 用 pwsh 7 不受影响）。

## 6. 报告漏洞

能导致 token 泄露、命令注入（例如把日志内容当命令执行）、权限提升的问题，
请用 **Security → Report a vulnerability**（私密 advisory），不要开公开 issue。
普通 bug 走 issue 模板即可。
