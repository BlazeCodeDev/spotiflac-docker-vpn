try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import hmac
import logging
import os
from functools import wraps

from flask import Flask, Response, request

import settings as _settings
import worker
import lib_index
import listenbrainz as _lb
from config import Config
from routes import bp

import applog
import vpn

_cfg = _settings.load()
applog.setup(_cfg.get("log_categories"))

os.makedirs(Config.OUTPUT_DIR, exist_ok=True)
worker.init(_cfg["max_workers"])
lib_index.start(lambda: Config.OUTPUT_DIR)
_lb.start()
vpn.start_watch()

import audiofp as _afp
_log = logging.getLogger("startup")
if _afp.unavailable_reason():
    _log.warning("Audio fingerprinting is off: %s", _afp.unavailable_reason())
applog.event("system", "Ready · port %s · %s workers · output %s%s" % (
    Config.PORT, _cfg["max_workers"], os.path.abspath(Config.OUTPUT_DIR),
    "" if Config.UI_PASSWORD else " · no UI password"))
_log.debug("services=%s vpn=%s fingerprinting=%s", _cfg["services"], Config.VPN_PROTOCOL,
           _afp.shutil.which("fpcalc") or "unavailable")

app = Flask(__name__, template_folder="templates")


# ── Basic Auth (optional — only active when UI_PASSWORD is set) ───────────────
def _require_auth():
    return Response(
        "Unauthorized", 401,
        {"WWW-Authenticate": 'Basic realm="SpotiFLAC"'},
    )


def _check_auth(req) -> bool:
    auth = req.authorization
    if not auth:
        return False
    return hmac.compare_digest(auth.password or "", Config.UI_PASSWORD)


def protected(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if Config.UI_PASSWORD and not _check_auth(request):
            return _require_auth()
        return f(*args, **kwargs)
    return wrapper


# Apply auth to all routes in the blueprint
@app.before_request
def auth_gate():
    if Config.UI_PASSWORD and not _check_auth(request):
        return _require_auth()


app.register_blueprint(bp)

if __name__ == "__main__":
    if Config.UI_PASSWORD:
        logging.getLogger(__name__).info("UI password protection active")
    app.run(host="0.0.0.0", port=Config.PORT, threaded=True)
