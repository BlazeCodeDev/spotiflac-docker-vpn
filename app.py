try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import logging
import os
from functools import wraps

from flask import Flask, Response, request

from config import Config
from routes import bp

logging.basicConfig(
    level=logging.INFO,
    format="[app] %(asctime)s %(message)s",
    datefmt="%H:%M:%S",
)

os.makedirs(Config.OUTPUT_DIR, exist_ok=True)

app = Flask(__name__, template_folder="templates")


# ── Basic Auth (optional — only active when UI_PASSWORD is set) ───────────────
def _require_auth():
    return Response(
        "Unauthorized", 401,
        {"WWW-Authenticate": 'Basic realm="SpotiFLAC"'},
    )


def _check_auth(req) -> bool:
    auth = req.authorization
    return bool(auth and auth.password == Config.UI_PASSWORD)


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
