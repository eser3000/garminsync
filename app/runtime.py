"""运行时单例：Garmin 服务与调度器（避免循环导入）。

iGPSPORT 服务不在运行时单例化：api / sync 统一通过
app.services.igpsport.get_service() 获取模块级单例。
"""
from app.db import SessionLocal
from app.services.garmin import GarminService
from app.services.scheduler import SyncScheduler

garmin_service = GarminService(SessionLocal)
scheduler = SyncScheduler(garmin_service, SessionLocal)
