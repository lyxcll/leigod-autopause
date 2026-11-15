"""端到端验证「游戏退出即暂停」：伪造雷神日志 + 用 notepad.exe 冒充游戏。

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
payload = {"id": "271", "status": "running", "option": {"game_id": 271, "game_name": "文明6"}}
with open(os.path.join(logs, "main-2099-01-01.log"), "w", encoding="utf-8") as f:
    f.write(f'[1234][{ts}.000] [info] notify [accelerating-status-change] '
            f'{json.dumps(payload, ensure_ascii=False)}\n')
with open(os.path.join(logs, "http-2099-01-01.log"), "w", encoding="utf-8") as f:
    f.write(f'[1234][{ts}.000] [info] GET https://webapi.leigod.com/api/user/status'
            f'?account_token={tok}&lang=zh_CN\n')

test_cfg = {
    "idle_minutes": 999, "min_accel_minutes": 0, "check_interval_seconds": 10,
    "only_when_client_running": True,
    "game_exe_names": ["notepad.exe"], "pause_on_game_exit": True,
    "game_exit_grace_seconds": 10, "extra_install_dirs": [fake],
}
shutil.copy(CFG, CFG + ".bak")
json.dump(test_cfg, open(CFG, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

# 2) 伪造「游戏正在跑」→ 然后关掉它，看是否立即触发暂停
np_ = subprocess.Popen(["notepad.exe"])
time.sleep(1)
p = subprocess.Popen([PY, os.path.join(BASE, "leigod_autopause.py"), "watch"],
                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=BASE)
time.sleep(16)
np_.kill()
print(f"[test] notepad 已关闭 @ {datetime.now():%H:%M:%S}", flush=True)
time.sleep(32)
p.terminate()
print(p.communicate(timeout=10)[0].decode("utf-8", "replace"))

shutil.move(CFG + ".bak", CFG)
shutil.rmtree(fake, ignore_errors=True)
