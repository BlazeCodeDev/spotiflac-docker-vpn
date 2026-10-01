try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import hmac
import logging
import os
import threading
import time
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


# ── Slow-request watch ────────────────────────────────────────────────────────
# When the web interface "hangs", requests are waiting on something (usually the music folder on a
# network share) and nothing is logged because nothing has failed yet. Name them while it happens.
# Streams that are meant to stay open are left out.
_inflight: dict = {}
_LONG_OK = ("/api/library/enrich-one", "/api/library/organize", "/api/library/download", "/api/jobs/")


def _req_label() -> str:
    q = request.query_string.decode(errors="replace")
    text = f"{request.method} {request.path}" + (f"?{q}" if q else "")
    return text if len(text) <= 110 else text[:109] + "…"


@app.before_request
def _track_start():
    request.environ["spotiflac.t0"] = time.monotonic()
    _inflight[threading.get_ident()] = (time.monotonic(), _req_label())


@app.teardown_request
def _track_end(_exc):
    _inflight.pop(threading.get_ident(), None)
    t0 = request.environ.get("spotiflac.t0")
    if t0 and not request.path.startswith(_LONG_OK):
        took = time.monotonic() - t0
        if took > 5:
            applog.event("system", f"Slow request · {_req_label()} · {took:.1f}s", logging.WARNING)


def _watch_requests() -> None:
    last = 0.0
    while True:
        time.sleep(10)
        now = time.monotonic()
        stuck = sorted(((now - t, label) for t, label in list(_inflight.values())), reverse=True)
        stuck = [(age, label) for age, label in stuck if age > 15 and not label.split(" ", 1)[1].startswith(_LONG_OK)]
        if stuck and now - last >= 30:
            last = now
            kinds = {}
            for _, label in stuck:
                path = label.split(" ", 1)[1].split("?", 1)[0]
                kinds[path] = kinds.get(path, 0) + 1
            applog.event("system", f"{len(stuck)} request{'s' if len(stuck) != 1 else ''} waiting for over 15s · "
                         + ", ".join(f"{n}× {p}" for p, n in sorted(kinds.items(), key=lambda kv: -kv[1])[:4])
                         + f" · oldest {stuck[0][0]:.0f}s: {stuck[0][1]}", logging.WARNING)


threading.Thread(target=_watch_requests, daemon=True, name="request-watch").start()

if __name__ == "__main__":
    if Config.UI_PASSWORD:
        logging.getLogger(__name__).info("UI password protection active")
    app.run(host="0.0.0.0", port=Config.PORT, threaded=True)
