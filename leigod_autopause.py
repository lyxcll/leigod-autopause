#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
雷神加速器（Leigod / NN）挂机自动暂停小工具 —— 单文件、只用标准库。

原理
----
1. 从雷神客户端自己的日志 D:\\LeiGod_Acc\\resources\\llogs\\http-*.log 里自动提取
   account_token（不需要手动复制 F12 抓包）。
2. 从 main-*.log 里解析 `notify [accelerating-status-change]` 事件，得到
   当前是否正在加速、加速的是哪个游戏、本次加速开始了多久。
3. 用 Windows GetLastInputInfo 判断键鼠空闲时长。
4. 满足「加速中 + 空闲 >= N 分钟 + 加速已持续 >= M 分钟」时，调用官方
   pause 接口暂停计时。

用法
----
  LeigodAutoPause.exe                    # 双击 → 菜单（exe 版）
  python leigod_autopause.py status      # 只读：看当前状态/是否会触发
  python leigod_autopause.py watch       # 常驻守护（开机自启用这个）
  python leigod_autopause.py pause       # 立即暂停一次
  python leigod_autopause.py install     # 注册开机自启（计划任务，失败则回退启动文件夹）
  python leigod_autopause.py uninstall   # 取消开机自启
  python leigod_autopause.py selftest    # 离线自检（不需要客户端/网络，CI 用）

配置：同目录 config.json（首次运行自动生成，目录不可写时改用 %LOCALAPPDATA%\\leigod-autopause）
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import glob
import hashlib
import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

def _app_dir() -> str:
    # PyInstaller 打包后 __file__ 在临时解包目录，配置/日志要跟着 exe 走
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _is_writable(path: str) -> bool:
    """目录能不能写（比如 exe 被放到 Program Files 里就不能）。"""
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, f".lg-write-test-{os.getpid()}")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        return True
    except Exception:
        return False


def _pick_base_dir() -> tuple[str, bool]:
    """优先用程序所在目录（便携）；不可写时退到 %LOCALAPPDATA%。"""
    cand = _app_dir()
    if _is_writable(cand):
        return cand, False
    fallback = os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "leigod-autopause"
    )
    try:
        os.makedirs(fallback, exist_ok=True)
    except Exception:
        pass
    return fallback, True


BASE_DIR, BASE_DIR_FALLBACK = _pick_base_dir()
VERSION = "1.2.4"
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
LOG_PATH = os.path.join(BASE_DIR, "autopause.log")
TOKEN_CACHE = os.path.join(BASE_DIR, ".token_cache")
TASK_NAME = "LeigodAutoPause"
# 全局单实例互斥量；测试可用环境变量换个名字，避免和真正在跑的守护互相干扰
MUTEX_NAME = os.environ.get("LEIGOD_AUTOPAUSE_MUTEX", r"Local\LeigodAutoPause")
_MUTEX = None
PAUSE_URL = "https://webapi.leigod.com/api/user/pause"
UA = "leigod/11.3.3.2"
TOKEN_RE = re.compile(r"account_token=([A-Za-z0-9_\-]{10,})")

# 控制台颜色（只有真正在终端里跑才开，重定向到文件时自动关掉）
_COLORS = {
    "reset": "\033[0m",
    "dim": "\033[2m",
    "bold": "\033[1m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "cyan": "\033[36m",
}
USE_COLOR = False
_ANSI_RE = re.compile(r"\033\[[0-9;]*m")

DEFAULT_CONFIG = {
    "idle_minutes": 15,            # 键鼠空闲多少分钟后允许暂停
    "min_accel_minutes": 3,        # 刚开加速的前几分钟不动（防止刚点就停）
    "check_interval_seconds": 30,  # 守护模式的检查间隔
    "only_when_client_running": True,
    "game_exe_names": [],          # 手动指定游戏主程序（留空 = 自动识别）
    "auto_detect_game_process": True,  # 从雷神日志里自动识别「当前加速的游戏」对应哪些进程
    "pause_on_game_exit": True,    # 游戏进程退出后立即暂停（不用等空闲阈值）
    "game_exit_grace_seconds": 90, # 退出后宽限几秒才暂停（防崩溃重启/退出来看攻略）
    "game_absent_pause_seconds": 300,  # 加速中但一直没见游戏在跑，多久后也暂停（0=关）
    "heartbeat_minutes": 30,       # 每多少分钟往日志里打一行心跳（0=不打）
    "extra_install_dirs": [],      # 可选：雷神装在别处时手动补充（优先使用）
}


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def setup_stdout() -> None:
    # pythonw.exe 拉起时 sys.stdout 可能是 None，直接 reconfigure 会炸
    for stream in ("stdout", "stderr"):
        s = getattr(sys, stream, None)
        try:
            if s is not None:
                s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def setup_console() -> None:
    """UTF-8 输出 + 打开 ANSI 颜色 + 设置窗口标题（仅在有控制台时）。"""
    global USE_COLOR
    setup_stdout()
    if sys.stdout is None:
        return
    try:
        if not sys.stdout.isatty():
            return
        k32 = ctypes.windll.kernel32
        handle = k32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if k32.GetConsoleMode(handle, ctypes.byref(mode)):
            k32.SetConsoleMode(handle, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
            USE_COLOR = os.environ.get("NO_COLOR", "") == ""
        k32.SetConsoleTitleW(f"雷神加速器 · 挂机自动暂停  v{VERSION}")
    except Exception:
        USE_COLOR = False


def c(text: str, color: str = "") -> str:
    """给终端文字上色（不在终端里时原样返回）。"""
    if not USE_COLOR or not color:
        return text
    return f"{_COLORS.get(color, '')}{text}{_COLORS['reset']}"


def self_sha256() -> str | None:
    """exe 自身的 SHA256；脚本模式返回 None。

    没有代码签名时，这是用户唯一能自己验证「下到的东西没被换过」的手段：
    对比 Release 里的 SHA256SUMS.txt 即可。
    """
    if not getattr(sys, "frozen", False):
        return None
    try:
        h = hashlib.sha256()
        with open(sys.executable, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def log(msg: str) -> None:
    # 任何一行落盘前先脱敏：异常信息、接口响应里都可能意外带出 token
    msg = redact(str(msg))
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    try:
        if sys.stdout is not None:      # 无控制台（pythonw）时跳过打印，只写文件
            print(line, flush=True)
    except Exception:
        pass
    try:
        # 日志滚动，避免无限增长
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 512 * 1024:
            with open(LOG_PATH, "r", encoding="utf-8", errors="ignore") as f:
                tail = f.readlines()[-800:]
            with open(LOG_PATH, "w", encoding="utf-8") as f:
                f.writelines(tail)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(_ANSI_RE.sub("", line) + "\n")
    except Exception:
        pass


def install_crash_hook() -> None:
    """兜底：未捕获异常也写进日志，别让进程静默死掉。"""

    def hook(exc_type, exc, tb) -> None:
        import traceback

        text = "".join(traceback.format_exception(exc_type, exc, tb))
        log(f"💥 崩溃：{exc_type.__name__}: {exc}")
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(text + "\n")
        except Exception:
            pass
        try:
            if sys.stderr is not None:
                sys.stderr.write(text)
        except Exception:
            pass

    sys.excepthook = hook


def load_config() -> dict:
    raw: dict = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                raise ValueError("config.json 顶层必须是对象")
        except Exception as e:
            log(f"⚠️ config.json 读取失败，使用默认值: {e}")
            raw = {}
    else:
        save_config(dict(DEFAULT_CONFIG))
        return dict(DEFAULT_CONFIG)

    missing = [k for k in DEFAULT_CONFIG if k not in raw]
    cfg = dict(DEFAULT_CONFIG)
    cfg.update({k: v for k, v in raw.items() if k in DEFAULT_CONFIG})
    if missing:
        save_config(cfg)
        log(f"🔧 配置已升级，补上 {len(missing)} 个新项：{', '.join(missing)}")
    return _clamp_config(cfg)


def _clamp_config(cfg: dict) -> dict:
    """把明显不合理的值拉回安全范围，别让手改配置把守护搞死。"""
    def as_int(key: str, lo: int, hi: int) -> None:
        try:
            cfg[key] = max(lo, min(hi, int(cfg[key])))
        except Exception:
            cfg[key] = DEFAULT_CONFIG[key]

    as_int("idle_minutes", 0, 24 * 60)
    as_int("min_accel_minutes", 0, 24 * 60)
    as_int("check_interval_seconds", 10, 3600)
    as_int("game_exit_grace_seconds", 0, 3600)
    as_int("game_absent_pause_seconds", 0, 24 * 3600)
    as_int("heartbeat_minutes", 0, 24 * 60)
    for key in ("only_when_client_running", "auto_detect_game_process",
                "pause_on_game_exit"):
        cfg[key] = bool(cfg.get(key, DEFAULT_CONFIG[key]))
    for key in ("game_exe_names", "extra_install_dirs"):
        val = cfg.get(key)
        cfg[key] = [str(x) for x in val] if isinstance(val, list) else []
    return cfg


def save_config(cfg: dict) -> None:
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"⚠️ config.json 写入失败: {e}")


def mask(token: str | None) -> str:
    """日志里只留前 4 位，够你认出是哪个号，又不足以被拿去用。"""
    if not token:
        return "(none)"
    return f"{token[:4]}*** (len={len(token)})"


# --------------------------------------------------------------------------- #
# 敏感数据：脱敏 / 文件 ACL / DPAPI 加密
# --------------------------------------------------------------------------- #
_SECRET_RES = (
    re.compile(r"(account_token=)[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(account_token\"?\s*[:=]\s*\"?)[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(github_pat_)[A-Za-z0-9_]{16,}"),
)
_TOKEN_CACHE_MAGIC = b"DPAPI1\n"


def redact(text: str) -> str:
    """把可能混进日志的凭证抹掉（响应体、异常信息里都可能带 token）。"""
    out = text
    for pat in _SECRET_RES:
        out = pat.sub(lambda m: m.group(1) + "***", out)
    return out


_ACL_WARNED = False


def harden_file(path: str) -> bool:
    """收紧文件 ACL：去掉继承来的 Users / Authenticated Users，只留本人 + SYSTEM + 管理员。

    Windows 默认继承会把「Authenticated Users」写成可改、Users 可读，
    放在 D:\\Tools、E:\\Edge 这类目录时，同一台机器的其他账户能直接读走 token。
    返回 True 表示已收紧（或无需处理）；失败只记一次警告，不静默。
    """
    global _ACL_WARNED
    if os.name != "nt" or not path:
        return True
    user = os.environ.get("USERNAME")
    if not user:
        return False
    if not os.path.exists(path):
        return False
    try:
        res = subprocess.run(
            ["icacls", path, "/inheritance:r",
             "/grant:r", f"{user}:F",
             "/grant:r", "*S-1-5-18:F",      # SYSTEM
             "/grant:r", "*S-1-5-32-544:F"],  # Administrators
            capture_output=True, timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if res.returncode == 0:
            return True
        if not _ACL_WARNED:
            _ACL_WARNED = True
            log(f"⚠️ 收紧 ACL 失败（icacls 退出码 {res.returncode}）：{path}"
                f" —— 同机其他账户可能读到这个文件")
        return False
    except Exception as e:  # noqa: BLE001
        if not _ACL_WARNED:
            _ACL_WARNED = True
            log(f"⚠️ 收紧 ACL 失败：{path}（{e.__class__.__name__}）")
        return False


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(protect: bool, data: bytes, entropy: bytes = b"leigod-autopause") -> bytes | None:
    """用 Windows DPAPI 做「只属于当前用户」的加解密（不用第三方库）。

    加密后的 token 只有同一个 Windows 账户能解开：别人拿到文件也用不了，
    拷到另一台机器更解不开。
    """
    if os.name != "nt" or not data:
        return None
    try:
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        buf_in = ctypes.create_string_buffer(data, len(data))
        buf_ent = ctypes.create_string_buffer(entropy, len(entropy))
        blob_in = _DATA_BLOB(len(data), ctypes.cast(buf_in, ctypes.POINTER(ctypes.c_char)))
        blob_ent = _DATA_BLOB(len(entropy), ctypes.cast(buf_ent, ctypes.POINTER(ctypes.c_char)))
        blob_out = _DATA_BLOB()
        fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
        ok = fn(ctypes.byref(blob_in), None, ctypes.byref(blob_ent), None, None,
                0x1,  # CRYPTPROTECT_UI_FORBIDDEN
                ctypes.byref(blob_out))
        if not ok:
            return None
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            kernel32.LocalFree(blob_out.pbData)
    except Exception:
        return None


_TOKEN_WARNED = False


def save_token(token: str) -> None:
    """缓存 token：优先 DPAPI 加密，再把文件 ACL 收紧到只剩本人。

    DPAPI 失败时退化写明文（否则断网/受限环境下无法暂停），但必须留下警告，
    不能让人以为磁盘上只有密文。
    """
    global _TOKEN_WARNED
    try:
        blob = _dpapi(True, token.encode("utf-8"))
        if blob is None:
            if not _TOKEN_WARNED:
                _TOKEN_WARNED = True
                log("⚠️ DPAPI 加密不可用，token 缓存以**明文**保存；已尽量收紧文件 ACL，"
                    "但同账户的程序仍可读取")
        data = (_TOKEN_CACHE_MAGIC + blob) if blob else token.encode("utf-8")
        with open(TOKEN_CACHE, "wb") as f:
            f.write(data)
        harden_file(TOKEN_CACHE)
    except OSError as e:
        log(f"⚠️ 写 token 缓存失败：{e}")


def load_cached_token() -> str | None:
    global _TOKEN_WARNED
    try:
        with open(TOKEN_CACHE, "rb") as f:
            raw = f.read()
    except OSError:
        return None
    if raw.startswith(_TOKEN_CACHE_MAGIC):
        plain = _dpapi(False, raw[len(_TOKEN_CACHE_MAGIC):])
        if plain is None:
            if not _TOKEN_WARNED:
                _TOKEN_WARNED = True
                log("⚠️ 无法解密 token 缓存（换过 Windows 账户/机器，或文件被改过），"
                    "将改从雷神日志重新读取")
            return None
        return plain.decode("utf-8", "replace")
    try:  # 旧版明文缓存：读出来后会重新加密
        return raw.decode("utf-8").strip() or None
    except UnicodeDecodeError:
        return None


# --------------------------------------------------------------------------- #
# 定位雷神安装目录
# --------------------------------------------------------------------------- #
def find_install_dirs(cfg: dict) -> list[str]:
    dirs: list[str] = []

    def add(p: str | None) -> None:
        if p and os.path.isdir(p) and p not in dirs:
            dirs.append(p)

    # 配置里指定的目录优先（方便测试 / 装在非常规位置时覆盖自动探测）
    for extra in cfg.get("extra_install_dirs") or []:
        add(extra)

    for hkey in (r"HKLM", r"HKCU"):
        for base in (
            rf"{hkey}\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
            rf"{hkey}\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
        ):
            try:
                out = subprocess.run(
                    ["reg", "query", base, "/s", "/f", "雷神", "/d"],
                    capture_output=True, timeout=10,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                ).stdout.decode("gbk", "ignore")
            except Exception:
                continue
            for line in out.splitlines():
                m = re.search(r"InstallLocation\s+REG_\w+\s+(.+)", line)
                if m:
                    add(m.group(1).strip().strip('"'))

    # 常见默认位置
    for guess in (
        r"D:\LeiGod_Acc", r"C:\Program Files\LeiGod_Acc", r"C:\Program Files (x86)\LeiGod_Acc",
        r"D:\Program Files\LeiGod_Acc", r"E:\LeiGod_Acc",
    ):
        add(guess)

    # 只要带 llogs 目录的
    return [d for d in dirs if os.path.isdir(os.path.join(d, "resources", "llogs"))]


# --------------------------------------------------------------------------- #
# 从日志读状态
# --------------------------------------------------------------------------- #
def newest_files(pattern: str, count: int) -> list[str]:
    files = glob.glob(pattern)
    files.sort(key=lambda p: os.path.getmtime(p))
    return files[-count:]


def read_token(install_dir: str) -> str | None:
    """从 http 日志里抓 account_token，取最新一条。"""
    logs = newest_files(os.path.join(install_dir, "resources", "llogs", "http-*.log"), 2)
    pat = TOKEN_RE
    for path in reversed(logs):
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for line in reversed(f.read().splitlines()):
                    m = pat.search(line)
                    if m:
                        token = m.group(1)
                        save_token(token)
                        return token
        except Exception:
            continue
    token = load_cached_token()
    if token:
        return token
    return None


EVENT_RE = re.compile(
    r"\[[0-9]+\]\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\.\d+\].*?"
    r"notify \[accelerating-status-change\] (?P<json>\{.*\})"
)

STOP_STATUSES = {"normal", "stop", "stopped", "failed"}

# 客户端每次「本地启动游戏」都会记一条，里面带着游戏自己的进程名，例如：
#   ipc handle launch-local-game with [{"game_id":110,"game_process":"RainbowSix.exe,RainbowSix_Vulkan.exe",...}]
# 用它就能自动知道「当前加速的游戏」关掉没，不用一游戏一配置。
LAUNCH_RE = re.compile(r"launch-local-game with (\[.*\])")
_GAME_PROC_CACHE: dict[str, tuple[float, int, dict[str, tuple[str, ...]]]] = {}


def _scan_launch_map(path: str) -> dict[str, tuple[str, ...]]:
    """解析单个主日志里的 launch-local-game 事件，返回 {game_id: (exe, ...)}。按 mtime 缓存。"""
    try:
        st = os.stat(path)
    except OSError:
        return {}
    cached = _GAME_PROC_CACHE.get(path)
    if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
        return cached[2]
    mapping: dict[str, tuple[str, ...]] = {}
    try:
        text = _decode(open(path, "rb").read())
    except Exception:
        text = ""
    for m in LAUNCH_RE.finditer(text):
        try:
            items = json.loads(m.group(1))
        except Exception:
            continue
        if isinstance(items, dict):
            items = [items]
        for it in items:
            if not isinstance(it, dict):
                continue
            gid = str(it.get("game_id") or "").strip()
            procs = tuple(p.strip() for p in str(it.get("game_process") or "").split(",")
                          if p.strip())
            if gid and procs:
                mapping[gid] = procs
    _GAME_PROC_CACHE[path] = (st.st_mtime, st.st_size, mapping)
    return mapping


def read_game_process_map(install_dir: str, days: int = 7) -> dict[str, tuple[str, ...]]:
    """把最近几天主日志里的 game_id -> 进程名 合起来。"""
    pattern = os.path.join(install_dir, "resources", "llogs", "main-*.log")
    files = sorted(glob.glob(pattern), key=lambda p: os.path.getmtime(p))[-days:]
    merged: dict[str, tuple[str, ...]] = {}
    for path in files:          # 后写的日志覆盖前面的
        merged.update(_scan_launch_map(path))
    return merged


def _decode(raw: bytes) -> str:
    """雷神日志有时 UTF-8、有时 GBK，两种都试。"""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("gbk", "ignore")


def _clean_game(name: str, game_id: str) -> str:
    # 客户端偶尔往日志里写坏的 UTF-8（含 U+FFFD），此时退回用 game_id 显示
    if not name or "\ufffd" in name:
        return f"游戏#{game_id}" if game_id else ""
    return name


def read_accel_state(install_dir: str, merge_gap_minutes: float = 10.0) -> dict:
    """
    返回 {accelerating, game, game_id, since, status}

    解析最近两个 main 日志。客户端切换/重连时会连续冒出
    normal -> before-start -> running，中间的空档小于 merge_gap_minutes
    时视为同一次加速，不重置计时起点。
    """
    state = {"accelerating": False, "game": "", "game_id": "", "since": None, "status": "unknown"}
    logs = newest_files(os.path.join(install_dir, "resources", "llogs", "main-*.log"), 2)
    events: list[tuple[datetime, str, str, str]] = []
    for path in logs:
        try:
            text = _decode(open(path, "rb").read())
        except Exception:
            continue
        for m in EVENT_RE.finditer(text):
            try:
                payload = json.loads(m.group("json"))
            except Exception:
                continue
            option = payload.get("option") or {}
            events.append((
                datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S"),
                str(payload.get("status", "")).lower(),
                str(option.get("game_id", "")),
                option.get("game_name") or "",
            ))
    events.sort(key=lambda e: e[0])

    episode_start: datetime | None = None
    episode_game = ""
    episode_gid = ""
    last_end: datetime | None = None
    for ts, status, gid, game in events:
        if status == "running":
            if episode_start is None:
                # 同一游戏在 merge_gap 内重启，算同一次加速
                if last_end and (ts - last_end).total_seconds() <= merge_gap_minutes * 60:
                    episode_start = last_end
                else:
                    episode_start = ts
                episode_game, episode_gid = game, gid
            state.update(accelerating=True, status=status,
                         game=_clean_game(episode_game, episode_gid), game_id=episode_gid)
        elif status in STOP_STATUSES:
            if episode_start is not None:
                last_end = ts
            episode_start = None
            state.update(accelerating=False, status=status, since=None)
        else:  # before-start 之类
            state["status"] = status
    state["since"] = episode_start if state["accelerating"] else None
    return state


# --------------------------------------------------------------------------- #
# 系统状态
# --------------------------------------------------------------------------- #
def idle_seconds() -> float:
    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]

    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
        return 0.0
    tick = ctypes.windll.kernel32.GetTickCount()
    delta = (tick - lii.dwTime) & 0xFFFFFFFF
    return delta / 1000.0


def process_running(names: list[str]) -> list[str]:
    """返回这些进程名里正在运行的（大小写不敏感，可省略 .exe）。"""
    if not names:
        return []
    wanted = {n.lower().removesuffix(".exe") for n in names}
    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        ).stdout.decode("gbk", "ignore")
    except Exception:
        return []
    found = []
    for line in out.splitlines():
        parts = line.split('","')
        if not parts:
            continue
        name = parts[0].strip('"').strip().lower().removesuffix(".exe")
        if name in wanted:
            found.append(name)
    return found


def client_running() -> bool:
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq leigod.exe", "/FO", "CSV", "/NH"],
            capture_output=True, timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        ).stdout.decode("gbk", "ignore")
        return "leigod.exe" in out.lower()
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# 暂停接口
# --------------------------------------------------------------------------- #
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """拒绝一切 3xx：token 在 POST 体里，跟随重定向可能把它带去别的主机。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def call_pause(token: str) -> dict:
    body = json.dumps({"account_token": token, "lang": "zh_CN"}).encode()
    req = urllib.request.Request(
        PAUSE_URL, data=body, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": UA},
    )
    # 不跟随重定向 + 强制 TLS 1.2 以上，并用系统默认 CA 校验证书
    ctx = ssl.create_default_context()
    try:
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    except (AttributeError, ValueError):
        pass
    opener = urllib.request.build_opener(
        _NoRedirect(), urllib.request.HTTPSHandler(context=ctx)
    )
    try:
        with opener.open(req, timeout=20) as resp:
            final = resp.geturl()
            raw = resp.read()
    except urllib.error.HTTPError as e:
        if 300 <= e.code < 400:
            return {"code": -3,
                    "msg": f"接口返回重定向 {e.code}，已拒绝跟随（防止 token 被带去其它主机）"}
        raw = e.read()
    except Exception as e:  # noqa: BLE001
        return {"code": -1, "msg": f"网络错误: {e}"}
    # 再确认一次：请求确实落在预期的主机上（http 层重定向之外的双保险）
    got = urllib.parse.urlsplit(final)
    want = urllib.parse.urlsplit(PAUSE_URL)
    if (got.scheme, got.netloc, got.path) != (want.scheme, want.netloc, want.path):
        return {"code": -3, "msg": f"响应来自非预期地址 {got.netloc}，已丢弃"}
    for enc in ("utf-8", "gbk"):
        try:
            return json.loads(raw.decode(enc))
        except Exception:
            continue
    return {"code": -2, "msg": f"无法解析响应: {raw[:120]!r}"}


def do_pause(install_dir: str, cfg: dict, reason: str) -> bool:
    token = read_token(install_dir)
    if not token:
        log("❌ 找不到 account_token（雷神客户端需要先登录过一次）")
        return False
    resp = call_pause(token)
    code = resp.get("code")
    msg = str(resp.get("msg", "")).strip()
    if code == 0:
        log(f"✅ 已暂停（{reason}）token={mask(token)} msg={msg}")
        return True
    if code == 400803:
        log(f"👌 已经是暂停状态（{reason}）code=400803 {msg}")
        return True
    if code == 400007:
        log("⚠️ 登录态失效（400007），尝试用最新日志里的 token 重试…")
        return False
    log(f"❌ 暂停失败 code={code} msg={msg}")
    return False


# --------------------------------------------------------------------------- #
# 状态展示
# --------------------------------------------------------------------------- #
def collect(install_dirs: list[str], cfg: dict) -> dict:
    install_dir = install_dirs[0] if install_dirs else ""
    state = read_accel_state(install_dir) if install_dir else {
        "accelerating": False, "game": "", "since": None, "status": "no-install"
    }
    gid = str(state.get("game_id") or "")
    exes: list[str] = []
    auto_used = False
    if install_dir and cfg.get("auto_detect_game_process", True) and gid:
        procs = read_game_process_map(install_dir).get(gid) or ()
        if procs:
            exes, auto_used = list(procs), True
    if not exes:
        exes = list(cfg.get("game_exe_names") or [])
    return {
        "install_dir": install_dir,
        "state": state,
        "idle": idle_seconds(),
        "client": client_running(),
        "game_ids": gid,
        "game_names": exes,
        "game_auto": auto_used,
        "game_wanted": bool(exes),
        "game_running": process_running(exes),
    }


def print_status(info: dict, cfg: dict) -> None:
    st = info["state"]
    line = "=" * 56
    print(c(line, "dim"))
    print(f"程序目录 : {BASE_DIR}"
          + (c("  （exe 目录不可写，已改用用户目录）", "yellow") if BASE_DIR_FALLBACK else ""))
    print(f"安装目录 : {info['install_dir'] or c('(未找到)', 'red')}")
    print(f"客户端   : {'运行中' if info['client'] else c('未运行', 'yellow')}")
    print(f"守护进程 : " + (c("运行中（后台自动守护）", "green") if watcher_alive()
                           else c("未运行 —— 双击 exe 选 1 安装", "yellow")))
    print(f"加速状态 : " + (c("加速中", "cyan") if st["accelerating"] else "未加速")
          + f" (log status={st['status']})")
    if st["accelerating"]:
        since = st["since"]
        dur = (datetime.now() - since).total_seconds() / 60 if since else 0
        print(f"当前游戏 : {st['game'] or '(未知)'}  已加速 {dur:.1f} 分钟")
    if info.get("game_wanted"):
        running = info.get("game_running") or []
        src = "自动识别" if info.get("game_auto") else "手动配置"
        print(f"游戏进程 : {'在跑 ' + ', '.join(running) if running else '没在跑'}"
              f"（{src}：{', '.join(info.get('game_names') or [])}）")
    print(f"键鼠空闲 : {info['idle'] / 60:.1f} 分钟（阈值 {cfg['idle_minutes']} 分钟）")
    will, why = should_pause(info, cfg)
    print(f"是否触发 : " + (c("会暂停", "yellow") if will else "不暂停") + f" —— {why}")
    print(c(line, "dim"))


def should_pause(info: dict, cfg: dict) -> tuple[bool, str]:
    st = info["state"]
    if cfg.get("only_when_client_running", True) and not info["client"]:
        return False, "雷神客户端没在运行"
    if not st["accelerating"]:
        return False, "当前没有在加速"
    if not info["install_dir"]:
        return False, "没找到雷神安装目录"
    idle_min = info["idle"] / 60.0
    if idle_min < cfg["idle_minutes"]:
        return False, f"空闲 {idle_min:.1f} 分钟 < {cfg['idle_minutes']} 分钟"
    since = st["since"]
    if since:
        accel_min = (datetime.now() - since).total_seconds() / 60.0
        if accel_min < cfg["min_accel_minutes"]:
            return False, f"刚加速 {accel_min:.1f} 分钟 < {cfg['min_accel_minutes']} 分钟"
    running = info.get("game_running") or []
    if running:
        return False, f"游戏进程还在: {', '.join(running)}"
    return True, "加速中 + 长时间没操作"


# --------------------------------------------------------------------------- #
# 命令
# --------------------------------------------------------------------------- #
def cmd_status(cfg: dict) -> int:
    install_dirs = find_install_dirs(cfg)
    info = collect(install_dirs, cfg)
    print_status(info, cfg)
    return 0


def cmd_pause(cfg: dict) -> int:
    install_dirs = find_install_dirs(cfg)
    if not install_dirs:
        log("❌ 没找到雷神安装目录，可在 config.json 的 extra_install_dirs 里补")
        return 1
    ok = do_pause(install_dirs[0], cfg, "手动")
    print_status(collect(install_dirs, cfg), cfg)
    return 0 if ok else 1


def _single_instance() -> bool:
    """用命名互斥量防止同时跑两个守护（开机自启 + 手动启动 / 重复安装）。"""
    global _MUTEX
    try:
        _MUTEX = ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
        return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS
    except Exception:
        return True


def watcher_alive() -> bool:
    """只看不占：有没有别的进程正跑着 watch（互斥量是否存在）。"""
    try:
        k32 = ctypes.windll.kernel32
        handle = k32.CreateMutexW(None, False, MUTEX_NAME)
        exists = k32.GetLastError() == 183
        if handle:
            k32.CloseHandle(handle)
        return exists
    except Exception:
        return False


def _note_duplicate_once(hours: float = 1.0) -> None:
    """别的实例（另一个装好的版本 / 另一个 exe）已经在守护时，最多每小时记一行。

    VBS 守护循环每 5 秒会重启一次，若每次退出都写日志，日志会被「已经在跑了」刷屏。
    """
    marker = os.path.join(BASE_DIR, ".duplicate_note")
    try:
        if os.path.exists(marker) and time.time() - os.path.getmtime(marker) < hours * 3600:
            return
        with open(marker, "w", encoding="utf-8") as f:
            f.write(str(int(time.time())))
        log("ℹ️ 已经有一个守护在跑了（可能是另一个装好的版本），本次启动退出")
    except OSError:
        pass


def cmd_watch(cfg: dict, dry_run: bool = False) -> int:
    if not _single_instance():
        _note_duplicate_once()
        return 0
    install_dirs = find_install_dirs(cfg)
    if not install_dirs:
        log("❌ 没找到雷神安装目录，可在 config.json 的 extra_install_dirs 里补")
        return 1
    install_dir = install_dirs[0]
    interval = max(10, int(cfg["check_interval_seconds"]))
    exit_watch = bool(cfg.get("pause_on_game_exit", True))
    log(f"👀 开始守护 v{VERSION}（pid {os.getpid()}）：{install_dir}，每 {interval}s 检查一次"
        f"（空闲阈值 {cfg['idle_minutes']} 分钟"
        f"{'，游戏退出即暂停' if exit_watch else ''}{'，dry-run' if dry_run else ''}）")
    if BASE_DIR_FALLBACK:
        log(f"ℹ️ 程序目录不可写，配置与日志放在：{BASE_DIR}")

    paused_since = None      # 这一轮加速是否已经处理过（按加速起始时间去重）
    game_seen = False        # 本轮加速里见到过游戏进程没
    gone_since = None        # 游戏进程从什么时候开始不见了
    episode_gid = ""         # 本轮加速的游戏 id（中途换游戏要重置判断）
    fail_count = 0
    last_beat = time.time()

    # 启动先打一行快照，方便事后判断「它到底看到了什么」
    try:
        snap = collect(install_dirs, cfg)
        st0 = snap["state"]
        running0 = snap.get("game_running") or []
        log("📊 当前："
            + ("加速中" + (f"（{st0['game']}）" if st0["game"] else "") if st0["accelerating"] else "未加速")
            + f"，游戏进程 {', '.join(running0) if running0 else '无'}"
            + ("（自动识别）" if snap.get("game_auto") else ""))
    except Exception:
        pass

    while True:
        running: list[str] = []
        try:
            info = collect(install_dirs, cfg)
            st = info["state"]
            gid = info.get("game_ids") or ""
            if not st["accelerating"]:
                paused_since = game_seen = gone_since = None
                episode_gid = ""
                fail_count = 0
            else:
                if paused_since is not None and st["since"] != paused_since:
                    # 客户端重新开始了一轮加速
                    paused_since = game_seen = gone_since = None
                if gid != episode_gid:
                    # 换游戏了，重新观察新游戏的进程
                    episode_gid = gid
                    game_seen, gone_since = False, None

                running = info.get("game_running") or []
                if running:
                    game_seen, gone_since = True, None
                elif game_seen and gone_since is None:
                    gone_since = datetime.now()

                reason = None
                if exit_watch and game_seen and not running and gone_since:
                    waited = (datetime.now() - gone_since).total_seconds()
                    if waited >= float(cfg.get("game_exit_grace_seconds", 90)):
                        reason = f"游戏已退出，无需再加速（{st['game'] or '游戏'}）"
                if reason is None and exit_watch and not game_seen and not running:
                    # 这一轮加速从头到尾就没见过游戏进程（关了软件才点加速 / 工具刚重启）
                    # 只有知道该盯哪些进程时才敢判断，否则宁可不乱停
                    absent = float(cfg.get("game_absent_pause_seconds", 300) or 0)
                    since = st["since"]
                    if absent > 0 and since and info.get("game_wanted"):
                        waited = (datetime.now() - since).total_seconds()
                        if waited >= absent:
                            reason = f"加速中但游戏没在跑，无需再加速（{st['game'] or '游戏'}）"
                if reason is None:
                    will, why = should_pause(info, cfg)
                    if will:
                        reason = why

                if reason and paused_since is None:
                    if dry_run:
                        log(f"🧪 [dry-run] 会暂停：{reason}")
                        paused_since = st["since"]
                    elif do_pause(install_dir, cfg, reason):
                        paused_since = st["since"]
                        fail_count = 0
                    else:
                        fail_count += 1
                        # 连续失败 5 次就放弃本轮，避免刷屏
                        if fail_count >= 5:
                            log("⚠️ 连续暂停失败，本次加速不再尝试")
                            paused_since = st["since"]

            hb = float(cfg.get("heartbeat_minutes", 30) or 0)
            if hb > 0 and time.time() - last_beat >= hb * 60:
                last_beat = time.time()
                log(f"💓 心跳：{'加速中' if st['accelerating'] else '未加速'}，"
                    f"游戏进程 {', '.join(running) if running else '无'}，"
                    f"空闲 {info['idle'] / 60:.0f} 分钟，"
                    f"{'本轮已处理' if paused_since is not None else '本轮未处理'}")
        except Exception as e:
            log(f"⚠️ 检查异常: {type(e).__name__}: {e}")
        time.sleep(interval)


def _pythonw() -> str:
    exe = sys.executable
    cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return cand if os.path.exists(cand) else exe


def _watch_cmd() -> str:
    """后台守护的启动命令行（打包后直接用 exe 自己）。"""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" watch'
    return f'"{_pythonw()}" "{os.path.abspath(__file__)}" watch'


def cmd_install(cfg: dict) -> int:
    cmd = _watch_cmd()
    startup = os.path.join(os.environ["APPDATA"],
                           r"Microsoft\Windows\Start Menu\Programs\Startup")
    link = os.path.join(startup, "LeigodAutoPause.vbs")

    # 旧实例（比如之前用 Python 版装的）先停掉，否则新实例会被互斥量挡住
    if watcher_alive():
        log("ℹ️ 发现有旧实例在跑，先停掉再装")
        _kill_running()

    # 1) 计划任务（登录时启动，无窗口）
    r = subprocess.run(
        ["schtasks", "/Create", "/F", "/SC", "ONLOGON", "/TN", TASK_NAME,
         "/TR", cmd, "/RL", "LIMITED"],
        capture_output=True,
    )
    if r.returncode == 0:
        log(f"✅ 已注册计划任务 {TASK_NAME}（登录时自动启动）")
        if os.path.exists(link):
            try:
                os.remove(link)
                log("🧹 已清理启动文件夹里的旧自启项（计划任务已接管）")
            except Exception:
                pass
    else:
        err = r.stderr.decode("gbk", "ignore").strip()[:200]
        log("⚠️ 计划任务创建失败：" + err)
        # 2) 回退：启动文件夹里的 VBS 守护循环
        #    VBScript 里字符串内的双引号要写成两个
        vbs_cmd = cmd.replace('"', '""')
        try:
            os.makedirs(os.path.dirname(link), exist_ok=True)
            with open(link, "w", encoding="gbk") as f:
                f.write('Set sh = CreateObject("WScript.Shell")\n')
                f.write("Do\n")
                f.write("  t0 = Timer\n")
                f.write(f'  sh.Run "{vbs_cmd}", 0, True\n')
                f.write("  dt = Timer - t0\n")
                f.write("  If dt < 0 Then dt = dt + 86400\n")
                # 秒退（说明已经有别的守护在跑）就退避到 60 秒，别每 5 秒拉一次进程
                f.write("  If dt < 60 Then WScript.Sleep 60000 Else WScript.Sleep 5000\n")
                f.write("Loop\n")
            log(f"✅ 已回退为启动文件夹自启：{link}")
        except Exception as e:
            log(f"❌ 自启安装失败: {e}")
            return 1

    legacy = os.path.join(BASE_DIR, "start_hidden.vbs")
    if os.path.exists(legacy):
        try:
            os.remove(legacy)
        except Exception:
            pass

    if getattr(sys, "frozen", False):
        log("提示：已开始后台守护，进度看 autopause.log（再双击 exe 可回到这个菜单）")
    else:
        log("提示：现在就能用 `python leigod_autopause.py watch` 手动跑起来")
    _spawn_watch_now()
    time.sleep(1.5)
    if watcher_alive():
        log("✅ 守护已在后台运行")
    else:
        log("⚠️ 守护没能起来，请看 autopause.log 最后几行")
    return 0


def _spawn_watch_now() -> None:
    """安装完立刻拉起一个守护，不用等下次登录（已有实例会被互斥量挡住）。"""
    DETACHED = 0x00000008
    try:
        if getattr(sys, "frozen", False):
            subprocess.Popen([sys.executable, "watch"], cwd=BASE_DIR,
                             creationflags=DETACHED, close_fds=True)
        else:
            subprocess.Popen([_pythonw(), os.path.abspath(__file__), "watch"],
                             cwd=BASE_DIR, creationflags=DETACHED, close_fds=True)
        log("🚀 已拉起后台守护")
    except Exception as e:
        log(f"⚠️ 拉起守护失败（下次登录仍会自动启动）: {e}")


def _kill_running() -> None:
    """结束正在跑的守护实例（只结束本工具的，不动别的进程）。"""
    if getattr(sys, "frozen", False):
        name = os.path.basename(sys.executable)
        # 排除自己：当前这个菜单进程正在执行卸载，不能把自己杀掉
        subprocess.run(["taskkill", "/F", "/IM", name,
                        "/FI", f"PID ne {os.getpid()}"], capture_output=True)
    # 兼容脚本版：pythonw.exe 里跑着 leigod_autopause.py 的实例也一起结束
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" | "
        "Where-Object { $_.CommandLine -like '*leigod_autopause.py*' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True)


def cmd_uninstall(cfg: dict) -> int:
    # 先让计划任务停止运行，再删除
    subprocess.run(["schtasks", "/End", "/TN", TASK_NAME], capture_output=True)
    r = subprocess.run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME], capture_output=True)
    if r.returncode == 0:
        log(f"✅ 已删除计划任务 {TASK_NAME}")
    else:
        log("ℹ️ 计划任务不存在或删除失败（可能本来就没装）")
    link = os.path.join(os.environ["APPDATA"],
                        r"Microsoft\Windows\Start Menu\Programs\Startup\LeigodAutoPause.vbs")
    if os.path.exists(link):
        try:
            os.remove(link)
            log("✅ 已删除启动文件夹自启项")
        except Exception as e:
            log(f"⚠️ 删除自启项失败: {e}")
    legacy = os.path.join(BASE_DIR, "start_hidden.vbs")
    if os.path.exists(legacy):
        try:
            os.remove(legacy)
        except Exception:
            pass
    # 自启项删了，但当前实例还在跑，得结束（否则它会继续守着）
    _kill_running()
    log("✅ 已结束正在运行的守护进程")
    log(f"ℹ️ 配置和日志仍保留在：{BASE_DIR}（想彻底清掉就手动删掉整个目录）")
    return 0


def _print_menu_header(cfg: dict) -> None:
    print()
    print(c("=" * 58, "dim"))
    print(c(f"  雷神加速器 · 挂机自动暂停   v{VERSION}", "bold"))
    print(f"  目录 {BASE_DIR}")
    try:
        info = collect(find_install_dirs(cfg), cfg)
        st = info["state"]
        accel = c(f"加速中（{st['game'] or '未知'}）", "cyan") if st["accelerating"] else "未加速"
        daemon = c("守护中", "green") if watcher_alive() else c("守护未运行", "yellow")
        where = info["install_dir"] or c("没找到雷神", "red")
        print(f"  状态 {accel} · {daemon} · {where}")
    except Exception:
        pass
    print(c("=" * 58, "dim"))


def _show_log_tail(lines: int = 20) -> None:
    if not os.path.exists(LOG_PATH):
        print("还没有日志（守护没跑过？双击选 1 装一下）")
        return
    try:
        with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            tail = f.readlines()[-lines:]
        print("".join(tail).rstrip())
    except Exception as e:
        print(f"读日志失败: {e}")


def _menu_exec(title: str, fn, cfg: dict) -> None:
    print()
    print(c(f"--- {title} ---", "cyan"))
    try:
        fn(cfg)
    except Exception as e:
        print(c(f"❌ 出错了: {type(e).__name__}: {e}", "red"))
    try:
        input(c("\n按回车返回菜单…", "dim"))
    except (EOFError, KeyboardInterrupt):
        raise SystemExit(0)


def cmd_menu(cfg: dict) -> int:
    """双击 exe 时的傻瓜菜单（不传参数才会进这里）。"""
    actions = {
        "1": ("安装 / 修复自启，并在后台守护", cmd_install),
        "2": ("刷新当前状态（只读，不会暂停）", cmd_status),
        "3": ("立即暂停一次（测试接口）", cmd_pause),
        "4": ("停止守护并取消开机自启", cmd_uninstall),
    }
    while True:
        _print_menu_header(cfg)
        print("  1) 安装 / 修复自启，并在后台守护   （推荐）")
        print("  2) 刷新当前状态（只读，不会暂停）")
        print("  3) 立即暂停一次（测试接口）")
        print("  4) 停止守护并取消开机自启")
        print("  5) 看日志（最近 20 行）")
        print("  6) 打开配置文件 config.json")
        print("  7) 打开程序目录")
        print("  0) 退出")
        print()
        try:
            choice = (input(c("请选择 [1-7,0]（直接回车 = 刷新状态）: ", "cyan")) or "2").strip()
        except (EOFError, KeyboardInterrupt):
            return 0
        if choice == "0":
            return 0
        if choice == "5":
            print()
            _show_log_tail()
            try:
                input(c("\n按回车返回菜单…", "dim"))
            except (EOFError, KeyboardInterrupt):
                return 0
            continue
        if choice == "6":
            try:
                if not os.path.exists(CONFIG_PATH):
                    save_config(cfg)
                os.startfile(CONFIG_PATH)
            except Exception as e:
                print(c(f"打不开配置文件: {e}", "red"))
            continue
        if choice == "7":
            try:
                os.startfile(BASE_DIR)
            except Exception as e:
                print(c(f"打不开目录: {e}", "red"))
            continue
        item = actions.get(choice)
        if not item:
            continue
        _menu_exec(item[0], item[1], cfg)


def cmd_selftest(cfg: dict) -> int:
    """离线自检：不碰客户端、不发请求，CI 里跑这个。"""
    import shutil
    import tempfile

    fails: list[str] = []

    def check(name: str, got, want) -> None:
        if got != want:
            fails.append(f"{name}: 期望 {want!r}，实际 {got!r}")

    tmp = tempfile.mkdtemp(prefix="lg-selftest-")
    try:
        llogs = os.path.join(tmp, "resources", "llogs")
        os.makedirs(llogs, exist_ok=True)
        main_log = os.path.join(llogs, "main-2026-01-02.log")
        with open(main_log, "w", encoding="utf-8") as f:
            f.write('[1234][2026-01-02 10:00:00.000] [info] notify [accelerating-status-change] '
                    '{"id":"110","status":"running","option":{"game_id":110,'
                    '"game_name":"彩虹六号:围攻 X"}}\n')
            f.write('[1234][2026-01-02 10:00:00.100] [info]  ipc handle launch-local-game with '
                    '[{"game_id":110,"game_process":"RainbowSix.exe,RainbowSix_Vulkan.exe"}]\n')

        st = read_accel_state(tmp)
        check("加速中", st["accelerating"], True)
        check("游戏名", st["game"], "彩虹六号:围攻 X")
        check("game_id", st["game_id"], "110")
        check("计时起点非空", st["since"] is not None, True)
        check("进程映射", read_game_process_map(tmp).get("110"),
              ("RainbowSix.exe", "RainbowSix_Vulkan.exe"))

        with open(main_log, "a", encoding="utf-8") as f:
            f.write('[1234][2026-01-02 10:30:00.000] [info] notify [accelerating-status-change] '
                    '{"id":"110","status":"normal","option":{"game_id":110,'
                    '"game_name":"彩虹六号:围攻 X"}}\n')
        check("停止后不再加速", read_accel_state(tmp)["accelerating"], False)

        # GBK 编码的旧日志也要能读
        gbk_log = os.path.join(llogs, "main-2026-01-03.log")
        with open(gbk_log, "wb") as f:
            f.write('[1234][2026-01-03 08:00:00.000] [info] notify [accelerating-status-change] '
                    '{"id":"271","status":"running","option":{"game_id":271,'
                    '"game_name":"文明6"}}\n'.encode("gbk"))
        check("GBK 日志可读", read_accel_state(tmp)["game"], "文明6")

        check("坏游戏名回退", _clean_game("����6", "271"), "游戏#271")
        m = TOKEN_RE.search("GET /api/user/info?account_token=abcDEF123456_xyz&lang=zh_CN")
        check("token 正则", m.group(1) if m else None, "abcDEF123456_xyz")
        for key in ("idle_minutes", "check_interval_seconds", "auto_detect_game_process"):
            check(f"配置项 {key}", key in cfg, True)

        clamped = _clamp_config({"idle_minutes": -5, "check_interval_seconds": 1,
                                 "game_exe_names": "oops", "pause_on_game_exit": "x"})
        check("空闲阈值下限", clamped["idle_minutes"], 0)
        check("轮询间隔下限", clamped["check_interval_seconds"], 10)
        check("列表字段容错", clamped["game_exe_names"], [])
        check("布尔字段容错", clamped["pause_on_game_exit"], True)
        check("帮助文本", "selftest" in HELP_TEXT, True)
        check("帮助含校验说明", "checksum" in HELP_TEXT, True)
        check("守护检测可用", callable(watcher_alive), True)
        check("自身校验可用", callable(self_sha256), True)
        check("日志脱敏", redact("x account_token=abcDEF123456_xyz y"), "x account_token=*** y")
        check("JSON 脱敏", redact('"account_token":"abcDEF123456_xyz"'),
              '"account_token":"***"')
        check("GitHub token 脱敏",
              redact("Authorization: Bearer github_pat_" + "A1" * 20).count("github_pat_***"), 1)
        blob = _dpapi(True, b"selftest-token")
        check("DPAPI 加密往返", _dpapi(False, blob) if blob else None, b"selftest-token")
        check("文件 ACL 加固可用", callable(harden_file), True)
        check("ACL 加固返回布尔", isinstance(harden_file(os.path.join(tmp, "nope.txt")), bool), True)
        # 接口只允许固定主机，且拒绝重定向（token 在 POST 体里，不能被带去别处）
        check("接口主机固定", urllib.parse.urlsplit(PAUSE_URL).netloc, "webapi.leigod.com")
        check("拒绝重定向",
              _NoRedirect().redirect_request(None, None, 302, "", {}, ""), None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if fails:
        for line in fails:
            print(f"❌ {line}")
        print(f"\n自检失败：{len(fails)} 项")
        return 1
    print("✅ 自检通过：日志解析 / 进程识别 / token 提取 / 配置读取 都正常")
    return 0


HELP_TEXT = f"""\
雷神加速器 · 挂机自动暂停  v{VERSION}

用法：
  LeigodAutoPause.exe                 打开菜单（推荐，双击就行）
  LeigodAutoPause.exe status          只看状态，会不会暂停、为什么
  LeigodAutoPause.exe watch           前台常驻守护（Ctrl+C 退出）
  LeigodAutoPause.exe watch --dry-run 演练模式，不真的调接口
  LeigodAutoPause.exe pause           立刻暂停一次
  LeigodAutoPause.exe install         注册开机自启并后台守护
  LeigodAutoPause.exe uninstall       取消自启并停止守护
  LeigodAutoPause.exe selftest        离线自检（不碰客户端/网络）
  LeigodAutoPause.exe checksum        打印自身 SHA256（对比 Release 的 SHA256SUMS.txt）
  LeigodAutoPause.exe --version       版本号 + 文件路径 + SHA256
  LeigodAutoPause.exe --help          这张帮助

配置：{CONFIG_PATH}
日志：{LOG_PATH}

说明：exe 没有商业代码签名（个人项目）。checksum 只能证明「下载完整、没被截断」
      （和 SHA256SUMS.txt 同源，被人同时替换两者就无效），不提供身份认证。
      想自己核对二进制是不是从这份源码构建的：见 SECURITY.md。EXE 首次运行会
      读雷神客户端的日志，token 只存在本机，并用 Windows DPAPI 按当前账户加密。
"""


def main(argv: list[str]) -> int:
    setup_console()
    install_crash_hook()
    cfg = load_config()
    # 配置和日志里可能有账号标识；收紧 ACL，别让同机其他账户读到
    harden_file(CONFIG_PATH)
    harden_file(LOG_PATH)
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd in ("--version", "-v", "version"):
        print(f"leigod-autopause {VERSION}")
        digest = self_sha256()
        if digest:
            print(f"文件   {sys.executable}")
            print(f"SHA256 {digest}")
            print("校验   与 Release 的 SHA256SUMS.txt 比对：一致 = 下载完整、没被截断/损坏")
        print("       但两者同源，挡不住「连 exe 和校验值一起换掉」；详见 SECURITY.md 的威胁模型")
        return 0
    if cmd in ("checksum", "sha256", "hash"):
        digest = self_sha256()
        if not digest:
            print("脚本模式下没有固定产物，exe 版才有自己的 SHA256")
            return 1
        print(digest)
        return 0
    if cmd in ("--help", "-h", "help"):
        print(HELP_TEXT)
        return 0
    if not cmd:
        # 双击 exe（没带参数）→ 菜单；命令行跑脚本 → 默认 status
        if getattr(sys, "frozen", False):
            return cmd_menu(cfg)
        cmd = "status"
    if cmd == "status":
        return cmd_status(cfg)
    if cmd == "pause":
        return cmd_pause(cfg)
    if cmd == "watch":
        return cmd_watch(cfg, dry_run="--dry-run" in argv)
    if cmd == "install":
        return cmd_install(cfg)
    if cmd == "uninstall":
        return cmd_uninstall(cfg)
    if cmd == "selftest":
        return cmd_selftest(cfg)
    print(HELP_TEXT)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except KeyboardInterrupt:
        print("\n已退出")
