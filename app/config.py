import os
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))

# 惰性创建：导入期不强制建目录，避免只读文件系统（或本地非 root）直接崩溃。
# 实际写入 DB 前由 ensure_dirs() 创建。
DB_PATH = DATA_DIR / "app.db"


def ensure_dirs() -> None:
    """在真正需要写文件前创建数据目录（仅当父路径可写时）。"""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        # 父目录不可写（如某些只读环境）；交由调用方在写失败时再报错。
        pass

# 同步频率合法档位（分钟）
FREQUENCY_OPTIONS = [10, 15, 30, 60, 120, 180, 240, 360]

# 同步时间范围（回溯时长）选项：键 -> 回溯天数
SYNC_WINDOW_OPTIONS = ["1day", "1week", "1month", "3months"]
SYNC_WINDOW_DAYS = {"1day": 1, "1week": 7, "1month": 30, "3months": 90}

# 健康数据同步回溯时长：固定近 1 个月（独立于活动同步窗口设置）
HEALTH_WINDOW_DAYS = 30

# MFA 验证码等待超时（秒）。前端登录后应在此时间内提交短信码。
MFA_WAIT_SECONDS = int(os.getenv("MFA_WAIT_SECONDS", "300"))

# 同一 region 两次登录请求的最小间隔（秒），避免频繁点击触发 Garmin 429 限流。
LOGIN_COOLDOWN_SECONDS = int(os.getenv("LOGIN_COOLDOWN_SECONDS", "60"))


