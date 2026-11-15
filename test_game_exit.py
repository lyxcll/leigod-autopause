"""端到端验证「游戏退出即暂停」：伪造雷神日志 + 用 ping.exe 冒充游戏。

日志里只放 launch-local-game 事件（进程名写在里面），所以走的是「自动识别」路径，
config 里 game_exe_names 留空 —— 即不针对任何具体游戏做配置。

会临时改写 config.json（结束自动还原）、会真的调一次 pause 接口（账号已暂停时返回 400803，无害）。
手动跑：python test_game_exit.py   总耗时约 60 秒。
"""
import json, os, shutil, subprocess, sys, tempfile, time
from datetime import datetime, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
CFG = os.path.join(BASE, "config.json")

# 1) 停掉真正的 watcher，避免它读到测试配置
subprocess.run(["taskkill", "/F", "/IM", "pythonw.exe"], capture_output=True)

tok = open(os.path.join(BASE, ".token_cache"), encoding="utf-8").read().strip()
fake = os.path.join(tempfile.gettempdir(), "lg_fake")
logs = os.path.join(fake, "resources", "llogs")
shutil.rmtree(fake, ignore_errors=True)
os.makedirs(logs, exist_ok=True)

ts = (datetime.now() - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
payload = {"id": "110", "status": "running", "option": {"game_id": 110, "game_name": "假游戏A"}}
launch = [{"game_id": 110, "game_process": "PING.EXE", "timeout": 90000}]
with open(os.path.join(logs, "main-2099-01-01.log"), "w", encoding="utf-8") as f:
    f.write(f'[1234][{ts}.000] [info] notify [accelerating-status-change] '
            f'{json.dumps(payload, ensure_ascii=False)}\n')
    f.write(f'[1234][{ts}.100] [info]  ipc handle launch-local-game with '
            f'{json.dumps(launch, ensure_ascii=False)}\n')
with open(os.path.join(logs, "http-2099-01-01.log"), "w", encoding="utf-8") as f:
    f.write(f'[1234][{ts}.000] [info] GET https://webapi.leigod.com/api/user/status'
            f'?account_token={tok}&lang=zh_CN\n')

test_cfg = {
    "idle_minutes": 999, "min_accel_minutes": 0, "check_interval_seconds": 10,
    "only_when_client_running": True,
    "game_exe_names": [], "auto_detect_game_process": True,
    "pause_on_game_exit": True, "game_exit_grace_seconds": 10,
    "game_absent_pause_seconds": 0, "extra_install_dirs": [fake],
}
shutil.copy(CFG, CFG + ".bak")
json.dump(test_cfg, open(CFG, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

# 2) 伪造「游戏正在跑」→ 然后关掉它，看是否立即触发暂停
game = subprocess.Popen([r"C:\Windows\System32\PING.EXE", "-n", "9999", "127.0.0.1"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        creationflags=subprocess.CREATE_NO_WINDOW)
time.sleep(1)
p = subprocess.Popen([PY, os.path.join(BASE, "leigod_autopause.py"), "watch"],
                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=BASE)
time.sleep(16)
game.kill()
print(f"[test] 假游戏 PING.EXE 已关闭 @ {datetime.now():%H:%M:%S}", flush=True)
time.sleep(32)
p.terminate()
print(p.communicate(timeout=10)[0].decode("utf-8", "replace"))

shutil.move(CFG + ".bak", CFG)
shutil.rmtree(fake, ignore_errors=True)
