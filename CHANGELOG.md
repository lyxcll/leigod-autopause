# 更新记录

版本号规则：`主.次.修订`。配置文件向后兼容，旧配置启动时自动补齐新字段。

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
