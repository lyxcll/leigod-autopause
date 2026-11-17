# 更新记录

版本号规则：`主.次.修订`。配置文件向后兼容，旧配置启动时自动补齐新字段。

## v1.3.2 — 便携版（更快）+ 清理残留

**新增免解压的便携版**

同一个 exe 现在有两种打包，功能完全一样，按需选一个：

| 项目 | 单文件（onefile） | 便携版（onedir） |
|---|---|---|
| 体积 | 8.1 MB | 8.2 MB（zip，解压后约 24 MB） |
| `--version` 启动 | 740 ms | **115 ms** |
| `status` | 969 ms | **388 ms** |
| `%TEMP%\_MEI*` | 每次运行解压一份 | **不产生** |

数字是本机 5 次 / 3 次中位数。便携版慢在「解压 8 MB 到临时目录」上：省掉这一步，
启动就只剩余正常的解释器初始化。发布页同时提供 `LeigodAutoPause-portable.zip`。

**新增 `clean-temp`（菜单 9）**

- `LeigodAutoPause.exe clean-temp` 清掉 `%TEMP%` 里 **12 小时前** 的 `_MEI*` 残留，
  打印删了几个、释放多少空间；被占用的会跳过，不算失败。
- `doctor` 增一行 `_MEI 临时残留`：有超过 12 小时的残留会提示，没超就 ✅。
- 阈值定 12 小时：正在运行的 onefile 进程会一直占着自己的目录（目录 mtime 不会更新），
  留足余量避免误删正在用的。

**CI 同时构建两个版本**

- `build_exe.yml` 里 `python build_exe.py` 出单文件，`python build_exe.py --onedir` 出便携版；
  两个都跑冒烟测试（`--version` + `selftest`），签名步骤对两个 exe 都生效，签名后重新打 zip，
  最后 `SHA256SUMS.txt` 同时列出 exe 与 zip 的哈希。

## v1.3.1 — 装完就自愈

修的是 v1.3.0 上线后暴露的「自启循环看着在跑，其实早就不干活了」。

**安装完立刻有自愈循环**

- `install` 写启动文件夹脚本之后会马上把循环拉起来（日志里那句
  `🔁 已启动自启守护循环（守护进程没了会 10 秒内拉回）`），不用等下次登录。
- 循环改成 **WMI 轮询**：每 10 秒数一遍「路径匹配 + 带 watch 参数」的守护进程，
  一个都没有才拉起。不再靠 `Run(..., 0, True)` 守着一个子进程——那种写法在子进程
  被强杀时可能永远不返回，循环就此卡死（v1.3.0 的真 bug）。

**三个让循环「假活」的坑（都已修）**

1. **WQL 转义**：把 `C:\Users\...` 直接塞进 `CommandLine LIKE '%...%'` 会因为
   WQL 里反斜杠是转义符而查询报错，错误又被前面那句 `On Error Resume Next` 吞掉，
   于是 `n` 永远算成「有一个进程」→ 永远不启动。改成按进程名查、路径匹配交给
   VBScript 的 `InStr`。
2. **看不见的模态对话框**：`wscript.exe` 无窗口运行时，未捕获的脚本错误会弹一个
   不可见的 Windows Script Host 对话框，循环永久停在那里等用户点「确定」。
   脚本第一行就是 `On Error Resume Next`，并且每一轮都把错误清掉。
3. **继承 `_MEIPASS`**（这次排查的根因）：从 `install`（PyInstaller onefile 进程）
   拉起的循环会继承 `_MEIPASS` / `_PYI_*`，`install` 退出后那个解压目录被删，
   循环之后再拉守护时，守护照着这个失效路径找自己的文件，静默失败——
   表现就是「循环进程活着，但永远拉不起新的守护」。现在脚本启动时先把这几个
   变量从进程环境里摘掉。

**顺手修**

- `install` 拉守护时也清掉 `_MEIPASS` 系列变量，减少 `%TEMP%\_MEI*` 残留
  （实测仍有少量残留，PyInstaller onefile 的清理依赖进程正常退出，无法根治）。
- `doctor` 的自启检查会比对当前 exe 路径，装在临时目录时会提示
  「自启项指向别的位置」。

## v1.3.0 — 更快、更小、自带体检

数字都是同一台机器、同一个 exe 包型（onefile）实测，v1.2.4 为基线：

| 项目 | v1.2.4 | v1.3.0 | 变化 |
|---|---|---|---|
| 体积 | 8,697,898 B | 8,526,016 B | **-2.0%** |
| `--version` 冷启动（7 次中位数） | 773 ms | **701 ms** | **-9%** |
| `status`（5 次中位数） | 1,895 ms | **944 ms** | **-50%** |

**新增 `doctor` 一键体检**

- `LeigodAutoPause.exe doctor`（菜单里是 `8`，别名 `check` / `diag`）逐项检查：
  程序目录可写、雷神安装目录、客户端进程、日志新鲜度、加速状态、游戏进程识别、
  token 是否可读且已加密、接口连通性（只建 TCP，不发 token）、开机自启指向、
  守护进程、日志近期活动。
- 每项 ✅ / ⚠️ / ❌，最后一行 `结论：N 项正常 · M 项注意 · K 项异常`，
  有异常时退出码为 1（方便脚本判断）。

**启动更快**

- `ssl` / `urllib` / `hashlib` 改成函数内延迟导入：`status`、菜单、`selftest`
  这些常用路径不再加载 TLS 栈（实测省 60-70 ms）。
- 进程枚举从 `tasklist` 子进程改为 **Toolhelp32 快照**（`CreateToolhelp32Snapshot`，
  本机 12 ms → 原 `tasklist` 250-670 ms），失败才退回 `tasklist`。
- `find_install_dirs()` 结果进程内缓存 60 秒（`reg query` 一次 200 ms+）；
  进程列表缓存 2 秒，`client_running()` 与 `process_running()` 不再各起一个子进程。

**体积**

- PyInstaller 排除表补上确实没用的 stdlib：`argparse` / `difflib` / `pprint` /
  `uuid` / `ftplib` / `imaplib` / `poplib` / `smtplib` / `telnetlib` / `nntplib` /
  `webbrowser` / `mimetypes` / `logging` / `gettext` / `pickle` / `xml` /
  `tarfile`；`zstd` / `lzma` / `bz2` / `decimal`。
- **两个不能动的坑（各踩了一次）**：
  - `unicodedata` 不能排除 —— `encodings.idna` 依赖它，去掉后所有 HTTPS 报
    `LookupError: unknown encoding: idna`；`selftest` 已加断言提前拦截。
  - `zipfile` 不能排除 —— PyInstaller 的 `pyi_rth_inspect` 启动时要 import 它，
    去掉后 exe 直接 `ModuleNotFoundError: No module named 'zipfile'`。

**自检**

- 新增「IDNA 编解码可用（HTTPS 前置）」「拒绝重定向」「接口主机固定」
  「ACL 加固返回布尔」等断言。

## v1.2.4 — 第二轮自审：不藏后门，也不藏「修不了」的部分

**网络（唯一出口）**

- `call_pause` 拒绝一切 3xx：token 在 POST 体里，跟随重定向可能把它带去别的主机；
  收到重定向直接返回错误，不发出第二次请求。
- 强制 TLS 1.2+、用系统默认 CA 校验证书；响应落地后再核对一次
  `scheme + netloc + path` 必须等于 `https://webapi.leigod.com/api/user/pause`。

**签名脚本（`scripts/sign_exe.ps1`）**

- 临时 `.pfx` 放进 `try/finally`：密码错、签名失败、校验失败都会删掉，不再残留私钥。
- 私钥改用 `EphemeralKeySet` 加载：不写进用户证书库、不能被再次导出。
- 临时证书文件 ACL 收紧到只留当前用户；时间戳默认用 DigiCert 的 `http://` 端点
  （Windows PowerShell 的 `Set-AuthenticodeSignature` 只接受 `http://` 形式，传 `https://` 会直接报错），
  签名失败时自动退回「不带时间戳签名」。

**测试脚本（`test_game_exit.py`）**

- 不再 `taskkill /F /IM pythonw.exe`（会误杀无关的 Python GUI 程序）。
- 测试全部在独立临时目录跑：复制一份脚本 + 自己的 `config.json` / 日志，**不碰真实安装**。
- token 只以 DPAPI 密文进入临时目录，日志里不再写明文 token；结束 `finally` 清理临时目录与进程。
- 支持 `LEIGOD_AUTOPAUSE_MUTEX` 环境变量换互斥量名字，避免和真守护抢单实例。

**不再静默失败**

- DPAPI 不可用 → 日志明确警告「token 缓存以明文保存」，不再假装加密了。
- `icacls` 收紧 ACL 失败 → 警告一次（不再 `except: pass`）。
- token 缓存解不开（换了 Windows 账户/机器）→ 警告一次，改从雷神日志重新读取。

**供应链与自检**

- CI 固定 `pyinstaller==6.22.3`、`pillow==13.0.0`（原本是浮动 `pip install`）。
- 自检新增：接口主机固定、重定向处理器返回 `None`、`harden_file` 返回布尔。

**审计记录**：单网络出口、无 `eval/exec/os.system/shell=True`、subprocess 全为 argv 形式、
exe 字符串扫描只有 `webapi.leigod.com` 与 PyInstaller manifest 的 `schemas.microsoft.com`、
git 全历史无真实凭证 —— 明细见 [SECURITY.md](SECURITY.md) 第 5 节。


## v1.2.3 — 安全加固：别把凭证摊在桌面上

**token 存储**

- `.token_cache` 改用 Windows **DPAPI** 按当前账户加密（`CryptProtectData`，无第三方库依赖）；
  旧版明文缓存会在读取后自动迁移成密文。拷到别的账户/机器 = 解不开。
- 收紧文件 ACL：`icacls /inheritance:r`，只留 当前用户 + SYSTEM + Administrators。
  Windows 默认继承会给 `Authenticated Users` 可改、`Users` 可读 —— 放在 `E:\Edge`、`D:\Tools`
  这类目录时，**同一台机器的其他账户原本能直接读走 token**（实测确认过）。
- `config.json` 与 `autopause.log` 启动时同样收紧 ACL。

**日志与输出**

- `log()` 落盘前统一脱敏：`account_token=…`、JSON 形式、`github_pat_…` 一律 `***`；
  异常信息和接口响应里的凭证也不会被写进日志。
- `mask()` 只保留前 4 位：`2a3G*** (len=27)`，不再显示末 3 位，`status` 可以放心贴到 issue。

**说清楚边界（不再含糊）**

- `checksum` / `SHA256SUMS.txt` 只证明「下载完整、没被截断」，**不是防篡改、不是身份认证**；
  它与 exe 同源，被同时替换就无效。`--version`、README、Release 说明全部改成这个措辞。
- `SECURITY.md` 重写：威胁模型、哪些防住了、哪些防不住（同账户恶意程序、管理员、内存取证）、
  为什么 PyInstaller 产物不可复现、`uninstall` 保留哪些文件。

**供应链**

- 所有 workflow 的 actions 从 `@v4`/`@v5` 固定到 **commit SHA**（注释标版本），避免 tag 被移动。
- 云端兜底 workflow 改用 `--data @-` 从 stdin 传 body，token 不再出现在 `curl` 进程命令行里。

## v1.2.2 — 守护循环不再刷日志 / 空转

- 已有别的守护在跑时（比如脚本版和 exe 版都装了），本次启动**静默退出**，最多每小时记一行提示 ——
  之前 VBS 每 5 秒拉一次进程，日志会被「已经有一个守护在跑了」刷满。
- VBS 守护循环加入退避：进程「秒退」（说明有别的守护）时改成 60 秒后再试，不再 5 秒拉一次；
  正常运行崩溃时仍按 5 秒快速重启。
- 说明：这两个问题只在「脚本版 + exe 版同时装过」的机器上暴露，普通安装无感。

## v1.2.1 — 没证书也能验，有证书就能签

**可自校验**

- 新增 `LeigodAutoPause.exe checksum`（别名 `sha256` / `hash`）：打印自身 SHA256；脚本模式下明确提示
  「只有 exe 有固定产物」并返回 1，不会假装自己有个哈希。
- `--version` 现在一起打印文件路径、SHA256 和一句校验说明。
- Release 页多带 `SHA256SUMS.txt`；CI 会拿 exe 自报的哈希跟文件实际哈希交叉核对，对不上直接构建失败。

**签名流水线（可选）**

- 新增 `scripts/sign_exe.ps1`：配了 `CODE_SIGN_PFX_BASE64` / `CODE_SIGN_PFX_PASSWORD` 就自动签名，
  没配就打印一句说明照常出未签名 exe。只用 Windows 自带的 `Set-AuthenticodeSignature`，不需要 Windows SDK / signtool。
- 签名在生成 `SHA256SUMS.txt` **之前**执行 —— 签名会改哈希，顺序反了就会发出对不上的校验值。
- 自签名证书实测：能签、签名后的 exe 仍能跑；但自签名不会改善 SmartScreen，README 里写清楚了这点。

**文档**

- README 新增「下载校验与代码签名」：为什么没签、30 秒手验、怎么接入真证书。

## v1.2.0 — exe 体验大修

**不会用命令行也能用明白了**

- 菜单重做：顶部实时显示「加速中 / 未加速 · 守护中 / 未运行 · 安装目录」，直接回车即可刷新状态；
  新增「看日志（最近 20 行）」「打开配置」「打开目录」，不用再自己去翻路径。
- 新增 `--help`（完整命令列表）和 `--version`；敲错命令也会打印帮助，而不是一片空白
  （打包后 docstring 会被优化掉，所以帮助文本改成常量）。
- `status` 增加「程序目录」和「守护进程」两行：一眼看出自启是不是活的、文件到底写在哪。

**更不容易坏**

- exe 所在目录不可写（例如放进 `C:\Program Files`）时，自动退到 `%LOCALAPPDATA%\leigod-autopause\`，
  不再因为写不了 `config.json` 直接失败。
- 配置自愈：缺字段自动补、类型不对自动纠正、数值越界自动夹到安全范围（负数、0 秒轮询之类都挡住），
  写盘失败只记日志不抛异常。
- `install` 会先停掉旧实例再装（脚本版和 exe 版混装也不会互相抢互斥量）；
  `uninstall` 先停掉计划任务再删、清理遗留 VBS，并且**结束守护时排除当前进程自己**（卸载不会把卸载器杀掉）。
- 未捕获异常统一写进 `autopause.log`（`💥 崩溃：…` + traceback），不再弹窗或无提示消失。
- 输出带颜色（仅交互终端；重定向/管道时自动纯文本），日志里不会混进 ANSI 转义符。

**更小更快**

- 打包排除 `setuptools/pip/distutils/xmlrpc/sqlite3/asyncio/multiprocessing/doctest/pdb` 等未用模块，
  并以 `PYTHONOPTIMIZE=2` 收集字节码：**8.9 MB → 8.7 MB**，构建时不再刷满日志。
- 版本信息补全 `Comments`、四段版本号，资源管理器属性里能看到版本与仓库地址。
- 构建脚本结尾直接打印体积与 SHA256，方便核对下载到的文件。

## v1.1.0 — 单文件 exe

- 新增 PyInstaller 打包：`LeigodAutoPause.exe`，双击即用，不需要装 Python。
- `config.json` / `autopause.log` 跟着 exe 走；`install` 后立刻在后台拉起守护。
- 新增 `build-exe.yml`：打 `v*` tag 自动构建、冒烟测试并附到 Release。

## v1.0.0 — 第一个可用版本

- 本地常驻守护：读雷神客户端日志拿登录态，按「键鼠空闲」或「游戏退出」自动调暂停接口。
- 从 `launch-local-game` 事件自动识别当前加速游戏的进程名，不用按游戏逐个配置。
- 开机自启：计划任务失败时回退到启动文件夹的 VBS 守护循环，被杀 5 秒内自愈；命名互斥量保证单实例。
- `selftest` 离线自检、`--dry-run` 演练、日志滚动、心跳，以及可选的云端每日兜底 workflow。
