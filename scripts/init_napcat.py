"""初始化 NapCat 的容器侧配置：WebUI 口令 + OneBot WS（带 token）。"""

from __future__ import annotations

import json
import pathlib
import secrets
import sys

sys.path.insert(0, "src")

from haiqbot.kernel.vault import Vault

cfg_dir = pathlib.Path(".dev/napcat/config")
cfg_dir.mkdir(parents=True, exist_ok=True)

webui_token = secrets.token_urlsafe(18)
onebot_token = secrets.token_urlsafe(24)

webui = {
    "host": "0.0.0.0",
    "prefix": "",
    "port": 6099,
    "token": webui_token,
    "loginRate": 3,
}

onebot = {
    "network": {
        "httpServers": [],
        "httpSseServers": [],
        "httpClients": [],
        "websocketServers": [
            {
                "enable": True,
                "name": "haiqbot-ws",
                "host": "0.0.0.0",
                "port": 3001,
                "reportSelfMessage": False,
                "enableForcePushEvent": True,
                "messagePostFormat": "array",
                "token": onebot_token,
                "debug": False,
                "heartInterval": 30000,
            }
        ],
        "websocketClients": [],
        "plugins": [],
    },
    "musicSignUrl": "",
    "enableLocalFile2Url": False,
    "parseMultMsg": False,
}

vault = Vault.default("secrets")
existing_webui = (cfg_dir / "webui.json").exists()

for name, data in (("webui.json", webui), ("onebot11.json", onebot)):
    path = cfg_dir / name
    if path.exists():
        print(f"skip（已存在，保留）: {path}")
        continue
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"写入: {path}")

if existing_webui:
    # 沿用文件里已有的口令，避免登录后令牌失效
    webui_token = json.loads((cfg_dir / "webui.json").read_text(encoding="utf-8"))["token"]
    onebot_token = json.loads((cfg_dir / "onebot11.json").read_text(encoding="utf-8"))["network"][
        "websocketServers"
    ][0]["token"]

vault.set("NAPCAT_WEBUI_TOKEN", webui_token)
vault.set("HAIQ_ONEBOT_ACCESS_TOKEN", onebot_token)

print("\n=== WebUI 登录口令（仅 127.0.0.1:6099 可达）===")
print(webui_token)
print("\n=== OneBot access_token ===")
print("已写入 haiq vault 键 HAIQ_ONEBOT_ACCESS_TOKEN（文件在 napcat/config/onebot11.json）")
