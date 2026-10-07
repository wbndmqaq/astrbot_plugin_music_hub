"""Music Hub 聚合点歌 —— QQ/酷狗/网易云三平台聚合音乐插件核心包。"""

from .help_data import VERSION as VERSION  # 版本号单一事实来源：help_data.py

PLUGIN_NAME = "astrbot_plugin_music_hub"
DISPLAY_NAME = "Music Hub"

SOURCE_NCM = "ncm"
SOURCE_KG = "kg"
SOURCE_QQ = "qq"

SOURCE_NAMES = {SOURCE_NCM: "网易云音乐", SOURCE_KG: "酷狗音乐", SOURCE_QQ: "QQ音乐"}

SOURCES = (SOURCE_NCM, SOURCE_KG, SOURCE_QQ)
