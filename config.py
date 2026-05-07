import os


class Config:
    OUTPUT_DIR   = os.environ.get("OUTPUT_DIR",   "./downloads")
    VPN_COUNTRY  = os.environ.get("VPN_COUNTRY",  "")
    VPN_PROTOCOL = os.environ.get("VPN_PROTOCOL", "openvpn")
    PORT         = int(os.environ.get("PORT",     "5000"))
    UI_PASSWORD  = os.environ.get("UI_PASSWORD",  "")
