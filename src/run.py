"""QuantDesk 主入口：启动数据引擎 + 本地 HTTP 工作台（兼容源码与 exe 运行）"""
import os, shutil, sys, threading, time, webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from quantdesk.paths import ensure_dirs, CONFIG_DIR, DEFAULT_CONFIG_DIR, APP_DIR
from quantdesk import engine, server
from quantdesk.config_loader import settings

def bootstrap_config():
    """首次运行：把打包内默认配置复制到 exe 旁的可写 config 目录"""
    ensure_dirs()
    for name in ("settings.json", "api_keys.json", "tradfi_symbols.json"):
        dst = os.path.join(CONFIG_DIR, name)
        src = os.path.join(DEFAULT_CONFIG_DIR, name)
        if not os.path.exists(dst) and os.path.exists(src):
            shutil.copy2(src, dst)
            print(f"[init] 已生成默认配置: {dst}")

def main():
    print("=" * 50)
    print(" QuantDesk · 币安 TradFi 量化工作台")
    print(f" 工作目录: {APP_DIR}")
    print("=" * 50)
    bootstrap_config()
    engine.start()
    srv = server.start_server()
    for mod_name, label in (("quantdesk.news", "舆情"), ("quantdesk.social", "社交情绪")):
        try:
            if mod_name == "quantdesk.news":
                from quantdesk import news as mod
                loop = mod.news_loop
            else:
                from quantdesk import social as mod
                loop = mod.social_loop
            threading.Thread(target=loop, daemon=True).start()
            print(f"[{mod_name}] {label}循环已启动")
        except ImportError:
            print(f"[{mod_name}] 模块未安装，跳过")
    port = settings.get("http_port", 8100)
    url = f"http://127.0.0.1:{port}"
    print(f"\n👉 工作台地址: {url}（3 秒后自动打开浏览器）\n")
    threading.Timer(3.0, lambda: webbrowser.open(url)).start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        srv.shutdown()
        print("已退出")

if __name__ == "__main__":
    main()
