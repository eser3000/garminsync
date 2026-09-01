"""FIT 文件生成（仅体重）。

使用 fit-tool 0.9.16 生成 WeightScaleMessage FIT 文件，
与 garmin-weight-sync 的 create_weight_fit_file 对齐：
- FileIdMessage: type=WEIGHT, manufacturer=GARMIN, product=INDEX_SMART_SCALE(2429)
- WeightScaleMessage: timestamp 传毫秒（fit-tool 内部按 date_time 偏移换算），
  weight 传 kg（内部按 scale=100 换算）
"""
import time

from fit_tool.fit_file_builder import FitFileBuilder
from fit_tool.profile.messages.file_id_message import FileIdMessage
from fit_tool.profile.messages.weight_scale_message import WeightScaleMessage
from fit_tool.profile.profile_type import FileType, GarminProduct, Manufacturer


def build_weight_fit(records, serial_number=88888888):
    """将体重记录列表生成一个 FIT 文件（多记录批量）。

    records: list[dict]，每条包含:
        timestamp(秒), weight(kg), 可选: bmi/body_fat_rate/moisture_rate/
        bone_mass/muscle_mass/metabolic_age/visceral_fat/basal_metabolism
    返回 FIT 字节。
    """
    builder = FitFileBuilder(auto_define=True, min_string_size=50)

    file_id = FileIdMessage()
    file_id.type = FileType.WEIGHT
    file_id.manufacturer = Manufacturer.GARMIN
    file_id.product = GarminProduct.INDEX_SMART_SCALE
    file_id.serial_number = serial_number
    file_id.time_created = int(time.time() * 1000)
    builder.add(file_id)

    for r in records:
        msg = WeightScaleMessage()
        msg.timestamp = int(r["timestamp"] * 1000)  # 毫秒
        msg.weight = r["weight"]  # kg
        if r.get("bmi"):
            msg.bmi = r["bmi"]
        if r.get("body_fat_rate"):
            msg.percent_fat = r["body_fat_rate"]
        if r.get("moisture_rate"):
            msg.percent_hydration = r["moisture_rate"]
        if r.get("bone_mass"):
            msg.bone_mass = r["bone_mass"]
        if r.get("muscle_mass"):
            msg.muscle_mass = r["muscle_mass"]
        if r.get("metabolic_age"):
            msg.metabolic_age = r["metabolic_age"]
        if r.get("visceral_fat"):
            msg.visceral_fat_rating = r["visceral_fat"]
        if r.get("basal_metabolism"):
            msg.basal_met = r["basal_metabolism"]
        builder.add(msg)

    return builder.build().to_bytes()
