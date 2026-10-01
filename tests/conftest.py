"""测试引导：把 AstrBot 框架源码与插件父目录放进 sys.path。

框架位置默认取仓库同级 ``AstrBot111``，可用环境变量 ``ASTRBOT_SRC`` 覆盖。
"""

import os
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
FRAMEWORK = Path(os.environ.get("ASTRBOT_SRC", PLUGIN_ROOT.parent / "AstrBot111"))

for p in (FRAMEWORK, PLUGIN_ROOT.parent):
    if p.is_dir() and str(p) not in sys.path:
        sys.path.insert(0, str(p))
