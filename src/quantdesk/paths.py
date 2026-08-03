"""统一路径解析：兼容源码运行与 PyInstaller 打包运行
- BUNDLE_DIR：只读资源（web/、config 默认模板）；打包时为 exe 内嵌目录
- APP_DIR：可写目录（config/、data/、reports/）；打包时为 exe 所在目录
"""
import os, sys

def _detect():
    if getattr(sys, "frozen", False):  # PyInstaller
        bundle = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
        app = os.path.dirname(sys.executable)
    else:
        app = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        bundle = app
    return bundle, app

BUNDLE_DIR, APP_DIR = _detect()
WEB_DIR = os.path.join(BUNDLE_DIR, "web")
DEFAULT_CONFIG_DIR = os.path.join(BUNDLE_DIR, "config")
CONFIG_DIR = os.path.join(APP_DIR, "config")
DATA_DIR = os.path.join(APP_DIR, "data")
REPORTS_DIR = os.path.join(APP_DIR, "reports")

def ensure_dirs():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(REPORTS_DIR, exist_ok=True)
