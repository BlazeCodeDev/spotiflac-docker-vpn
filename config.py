import os


class Config:
    OUTPUT_DIR   = os.environ.get("OUTPUT_DIR",            "./downloads")
    SERVICES     = [s.strip() for s in os.environ.get("SPOTIFLAC_SERVICES", "tidal,qobuz,amazon,youtube").split(",") if s.strip()]
    FILENAME_FMT = os.environ.get("FILENAME_FORMAT",       "{artist}/{album}/{track} {title}")
    ARTIST_DIRS  = os.environ.get("USE_ARTIST_SUBFOLDERS", "false").lower() == "true"
    ALBUM_DIRS   = os.environ.get("USE_ALBUM_SUBFOLDERS",  "false").lower() == "true"
    RETRY_MIN     = int(os.environ.get("RETRY_MINUTES",      "0"))
    TRACK_DELAY_S = float(os.environ.get("TRACK_DELAY_SECONDS", "4.0"))
    MAX_WORKERS  = int(os.environ.get("MAX_WORKERS",       "3"))
    QOBUZ_TOKEN  = os.environ.get("QOBUZ_TOKEN",           "")
    VPN_COUNTRY  = os.environ.get("VPN_COUNTRY",           "")
    VPN_PROTOCOL = os.environ.get("VPN_PROTOCOL",          "openvpn")
    PORT         = int(os.environ.get("PORT",              "5000"))
    UI_PASSWORD  = os.environ.get("UI_PASSWORD",           "")
