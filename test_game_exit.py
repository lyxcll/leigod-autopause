"""端到端验证「游戏退出即暂停」：伪造雷神日志 + 用 ping.exe 冒充游戏。

安全约束（审计后重写）：
- **不再** taskkill 掉全机 pythonw，也不碰真实的 config.json / autopause.log；
  所有测试文件都在独立的临时目录里，测试结束整体删除。
- token 只以 DPAPI 密文形式放进临时目录，绝不把明文写进日志/文件。
- 用 try/finally 保证临时目录和子进程一定被清理。

日志里只放 launch-local-game 事件（进程名写在里面），所以走的是「自动识别」路径，
不针对任何具体游戏做配置。

手动跑：python test_game_exit.py   总耗时约 35 秒，结束时打印 watcher 输出。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import leigod_autopause as la  # noqa: E402

# Windows 控制台默认 GBK，遇到 emoji/ℹ 会 UnicodeEncodeError；统一按 UTF-8 输出
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def make_fake_logs(logs: str) -> None:
    ts = (datetime.now() - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    payload = {"id": "110", "status": "running",
               "option": {"game_id": 110, "game_name": "假游戏A"}}
    launch = [{"game_id": 110, "game_process": "PING.EXE", "timeout": 90000}]
    with open(os.path.join(logs, "main-2099-01-01.log"), "w", encoding="utf-8") as f:
        f.write(f'[1234][{ts}.000] [info] notify [accelerating-status-change] '
                f'{json.dumps(payload, ensure_ascii=False)}\n')
        f.write(f'[1234][{ts}.100] [info]  ipc handle launch-local-game with '
                f'{json.dumps(launch, ensure_ascii=False)}\n')


def seed_token(work: str) -> bool:
    """把本机 token 以 DPAPI 密文放进临时目录；取不到就返回 False（跳过真实调用）。"""
    install = next(iter(la.find_install_dirs({})), None)
    token = la.load_cached_token() or (la.read_token(install) if install else None)
    if not token:
        return False
    blob = la._dpapi(True, token.encode("utf-8"))
    if blob is None:      # 没有 DPAPI 就不测真实调用，避免明文落盘
        return False
    with open(os.path.join(work, os.path.basename(la.TOKEN_CACHE)), "wb") as f:
        f.write(la._TOKEN_CACHE_MAGIC + blob)
    return True


def main() -> int:
    work = tempfile.mkdtemp(prefix="lgtest_")
    fake = os.path.join(work, "fake")
    logs = os.path.join(fake, "resources", "llogs")
    os.makedirs(logs, exist_ok=True)
    proc = None
    game = None
    try:
        shutil.copy(la.__file__, os.path.join(work, "leigod_autopause.py"))
        make_fake_logs(logs)
        has_token = seed_token(work)
        print(f"[test] 临时目录 {work}，token={'DPAPI 密文已就位' if has_token else '缺失（只验证触发，不看接口结果）'}")

        cfg = {
            "idle_minutes": 999, "min_accel_minutes": 0, "check_interval_seconds": 5,
            "only_when_client_running": False,
            "game_exe_names": [], "auto_detect_game_process": True,
            "pause_on_game_exit": True, "game_exit_grace_seconds": 5,
            "game_absent_pause_seconds": 0, "extra_install_dirs": [fake],
            "heartbeat_minutes": 0,
        }
        with open(os.path.join(work, "config.json"), "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)

        ping = shutil.which("ping") or r"C:\Windows\System32\PING.EXE"
        game = subprocess.Popen([ping, "-n", "9999", "127.0.0.1"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        time.sleep(1)
        proc = subprocess.Popen(
            [sys.executable, os.path.join(work, "leigod_autopause.py"), "watch"],
            cwd=work, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW,
            env={**os.environ, "LEIGOD_AUTOPAUSE_MUTEX": r"Local\LeigodAutoPauseTest"},
        )
        time.sleep(9)          # 让 watcher 看到「游戏在跑」
        game.kill()
        game = None
        print(f"[test] 假游戏 PING.EXE 已关闭 @ {datetime.now():%H:%M:%S}", flush=True)
        # 检查间隔下限 10s（_clamp_config），加上宽限 5s，至少留两轮
        time.sleep(28)
        proc.terminate()
        out = proc.communicate(timeout=10)[0].decode("utf-8", "replace")
        print(out)
        proc = None
        log_file = os.path.join(work, "autopause.log")
        if os.path.exists(log_file):
            print("[test] ---- autopause.log ----")
            print(open(log_file, encoding="utf-8", errors="replace").read())
            out += open(log_file, encoding="utf-8", errors="replace").read()

        ok = "游戏已退出" in out
        print(f"[test] {'✅ PASS' if ok else '❌ FAIL'}："
              f"{'检测到退出并触发暂停' if ok else '没有捕获到退出事件'}")
        return 0 if ok else 1
    finally:
        for p in (proc, game):
            if p is not None:
                try:
                    p.kill()
                except Exception:
                    pass
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
