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
  python leigod_autopause.py status      # 只读：看当前状态/是否会触发
  python leigod_autopause.py watch       # 常驻守护（开机自启用这个）
  python leigod_autopause.py pause       # 立即暂停一次
  python leigod_autopause.py install     # 注册开机自启（计划任务，失败则回退启动文件夹）
  python leigod_autopause.py uninstall   # 取消开机自启

配置：同目录 config.json（首次运行自动生成）
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import glob
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
LOG_PATH = os.path.join(BASE_DIR, "autopause.log")
TOKEN_CACHE = os.path.join(BASE_DIR, ".token_cache")
TASK_NAME = "LeigodAutoPause"
PAUSE_URL = "https://webapi.leigod.com/api/user/pause"
UA = "leigod/11.3.3.2"

DEFAULT_CONFIG = {
    "idle_minutes": 15,            # 键鼠空闲多少分钟后允许暂停
    "min_accel_minutes": 3,        # 刚开加速的前几分钟不动（防止刚点就停）
    "check_interval_seconds": 30,  # 守护模式的检查间隔
    "only_when_client_running": True,
    "game_exe_names": [],          # 游戏主程序，例如 ["CivilizationVI.exe", "CivilizationVI_DX12.exe"]
    "pause_on_game_exit": False,   # 游戏进程退出后立即暂停（不用等空闲阈值）
    "game_exit_grace_seconds": 90, # 退出后宽限几秒才暂停（防崩溃重启/退出来看攻略）
    "extra_install_dirs": [],      # 可选：雷神装在别处时手动补充（优先使用）
}


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def setup_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def log(msg: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        # 日志滚动，避免无限增长
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 512 * 1024:
            with open(LOG_PATH, "r", encoding="utf-8", errors="ignore") as f:
                tail = f.readlines()[-800:]
            with open(LOG_PATH, "w", encoding="utf-8") as f:
                f.writelines(tail)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
                cfg.update(json.load(f))
        except Exception as e:
            log(f"⚠️ config.json 读取失败，使用默认值: {e}")
    else:
        save_config(cfg)
    return cfg


def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def mask(token: str | None) -> str:
    if not token:
        return "(none)"
    return f"{token[:4]}***{token[-3:]}(len={len(token)})"


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
    pat = re.compile(r"account_token=([A-Za-z0-9_\-]{10,})")
    for path in reversed(logs):
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for line in reversed(f.read().splitlines()):
                    m = pat.search(line)
                    if m:
                        token = m.group(1)
                        try:
                            with open(TOKEN_CACHE, "w", encoding="utf-8") as tf:
                                tf.write(token)
                        except Exception:
                            pass
                        return token
        except Exception:
            continue
    try:
        if os.path.exists(TOKEN_CACHE):
            return open(TOKEN_CACHE, "r", encoding="utf-8").read().strip() or None
    except Exception:
        pass
    return None


EVENT_RE = re.compile(
    r"\[[0-9]+\]\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\.\d+\].*?"
    r"notify \[accelerating-status-change\] (?P<json>\{.*\})"
)

STOP_STATUSES = {"normal", "stop", "stopped", "failed"}


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
def call_pause(token: str) -> dict:
    body = json.dumps({"account_token": token, "lang": "zh_CN"}).encode()
    req = urllib.request.Request(
        PAUSE_URL, data=body, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raw = e.read()
    except Exception as e:
        return {"code": -1, "msg": f"网络错误: {e}"}
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
    games = cfg.get("game_exe_names") or []
    return {
        "install_dir": install_dir,
        "state": state,
        "idle": idle_seconds(),
        "client": client_running(),
        "game_wanted": bool(games),
        "game_running": process_running(games),
    }


def print_status(info: dict, cfg: dict) -> None:
    st = info["state"]
    print("=" * 56)
    print(f"安装目录 : {info['install_dir'] or '(未找到)'}")
    print(f"客户端   : {'运行中' if info['client'] else '未运行'}")
    print(f"加速状态 : {'加速中' if st['accelerating'] else '未加速'} (log status={st['status']})")
    if st["accelerating"]:
        since = st["since"]
        dur = (datetime.now() - since).total_seconds() / 60 if since else 0
        print(f"当前游戏 : {st['game'] or '(未知)'}  已加速 {dur:.1f} 分钟")
    if info.get("game_wanted"):
        running = info.get("game_running") or []
        print(f"游戏进程 : {'在跑 ' + ', '.join(running) if running else '没在跑'}"
              f"{'（退出即暂停）' if cfg.get('pause_on_game_exit') else ''}")
    print(f"键鼠空闲 : {info['idle'] / 60:.1f} 分钟（阈值 {cfg['idle_minutes']} 分钟）")
    will, why = should_pause(info, cfg)
    print(f"是否触发 : {'会暂停' if will else '不暂停'} —— {why}")
    print("=" * 56)


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


def cmd_watch(cfg: dict, dry_run: bool = False) -> int:
    install_dirs = find_install_dirs(cfg)
    if not install_dirs:
        log("❌ 没找到雷神安装目录，可在 config.json 的 extra_install_dirs 里补")
        return 1
    install_dir = install_dirs[0]
    interval = max(10, int(cfg["check_interval_seconds"]))
    exit_watch = bool(cfg.get("pause_on_game_exit")) and bool(cfg.get("game_exe_names"))
    log(f"👀 开始守护：{install_dir}，每 {interval}s 检查一次"
        f"（空闲阈值 {cfg['idle_minutes']} 分钟"
        f"{'，游戏退出即暂停' if exit_watch else ''}{'，dry-run' if dry_run else ''}）")

    paused_since = None      # 这一轮加速是否已经处理过（按加速起始时间去重）
    game_seen = False        # 本轮加速里见到过游戏进程没
    gone_since = None        # 游戏进程从什么时候开始不见了
    fail_count = 0
    while True:
        try:
            info = collect(install_dirs, cfg)
            st = info["state"]
            if not st["accelerating"]:
                paused_since = game_seen = gone_since = None
                fail_count = 0
            else:
                if paused_since is not None and st["since"] != paused_since:
                    # 客户端重新开始了一轮加速
                    paused_since = game_seen = gone_since = None

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
        except Exception as e:
            log(f"⚠️ 检查异常: {type(e).__name__}: {e}")
        time.sleep(interval)


def _pythonw() -> str:
    exe = sys.executable
    cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return cand if os.path.exists(cand) else exe


def cmd_install(cfg: dict) -> int:
    script = os.path.abspath(__file__)
    cmd = f'"{_pythonw()}" "{script}" watch'
    # 1) 计划任务（登录时启动，无窗口）
    r = subprocess.run(
        ["schtasks", "/Create", "/F", "/SC", "ONLOGON", "/TN", TASK_NAME,
         "/TR", cmd, "/RL", "LIMITED"],
        capture_output=True,
    )
    if r.returncode == 0:
        log(f"✅ 已注册计划任务 {TASK_NAME}（登录时自动启动）")
    else:
        log("⚠️ 计划任务创建失败：" + r.stderr.decode("gbk", "ignore").strip()[:200])
        # 2) 回退：启动文件夹快捷方式
        startup = os.path.join(os.environ["APPDATA"],
                               r"Microsoft\Windows\Start Menu\Programs\Startup")
        vbs = os.path.join(BASE_DIR, "start_hidden.vbs")
        # VBScript 里字符串内的双引号要写成两个
        vbs_cmd = cmd.replace('"', '""')
        with open(vbs, "w", encoding="gbk") as f:
            f.write(f'CreateObject("WScript.Shell").Run "{vbs_cmd}", 0, False\r\n')
        link = os.path.join(startup, "LeigodAutoPause.vbs")
        try:
            import shutil
            shutil.copy(vbs, link)
            log(f"✅ 已回退为启动文件夹自启：{link}")
        except Exception as e:
            log(f"❌ 自启安装失败: {e}")
            return 1
    log("提示：现在就能用 `python leigod_autopause.py watch` 手动跑起来")
    return 0


def cmd_uninstall(cfg: dict) -> int:
    r = subprocess.run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME], capture_output=True)
    if r.returncode == 0:
        log(f"✅ 已删除计划任务 {TASK_NAME}")
    else:
        log("ℹ️ 计划任务不存在或删除失败（可能本来就没装）")
    link = os.path.join(os.environ["APPDATA"],
                        r"Microsoft\Windows\Start Menu\Programs\Startup\LeigodAutoPause.vbs")
    if os.path.exists(link):
        os.remove(link)
        log("✅ 已删除启动文件夹自启项")
    return 0


def main(argv: list[str]) -> int:
    setup_stdout()
    cfg = load_config()
    cmd = argv[1] if len(argv) > 1 else "status"
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
    print(__doc__)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except KeyboardInterrupt:
        print("\n已退出")
