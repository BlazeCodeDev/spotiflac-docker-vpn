"""Open-file-descriptor watchdog: finds what is leaking when the process runs out of descriptors.

A background thread samples /proc/self/fd once a minute. When usage is high or has jumped, it logs a
WARNING (always printed) with the descriptors grouped by kind, and sockets grouped by remote address.
`snapshot()` is also served at /api/debug/fds.
"""
import collections
import logging
import os
import resource
import socket
import struct
import threading
import time

log = logging.getLogger("fdwatch")
_INTERVAL = 60
_WARN_FRACTION = 0.5
_WARN_JUMP = 200


def _socket_peers() -> dict:
    """inode -> 'ip:port' for every TCP socket the kernel knows about."""
    peers = {}
    for path, v6 in (("/proc/net/tcp", False), ("/proc/net/tcp6", True)):
        try:
            with open(path) as f:
                next(f)
                for line in f:
                    p = line.split()
                    hexip, hexport = p[2].split(":")
                    raw = bytes.fromhex(hexip)
                    if v6:
                        ip = socket.inet_ntop(socket.AF_INET6, struct.pack("<4I", *struct.unpack("<4I", raw)))
                    else:
                        ip = socket.inet_ntop(socket.AF_INET, raw[::-1])
                    peers[p[9]] = "%s:%d" % (ip, int(hexport, 16))
        except (OSError, StopIteration, ValueError):
            pass
    return peers


def snapshot() -> dict:
    soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    kinds, sockets, files = collections.Counter(), collections.Counter(), collections.Counter()
    peers = None
    try:
        names = os.listdir("/proc/self/fd")
    except OSError:
        names = []
    for fd in names:
        try:
            t = os.readlink("/proc/self/fd/" + fd)
        except OSError:
            continue
        if t.startswith("socket:["):
            kinds["socket"] += 1
            if peers is None:
                peers = _socket_peers()
            sockets[peers.get(t[8:-1], "unconnected/unix")] += 1
        elif t.startswith("pipe:"):
            kinds["pipe"] += 1
        elif t.startswith("anon_inode:"):
            kinds[t] += 1
        else:
            kinds["file"] += 1
            files[t] += 1
    return {"open": len(names), "limit": soft, "kinds": dict(kinds),
            "top_sockets": sockets.most_common(10), "top_files": files.most_common(10),
            "threads": threading.active_count()}


def _loop() -> None:
    last = 0
    while True:
        time.sleep(_INTERVAL)
        try:
            s = snapshot()
            if s["open"] >= s["limit"] * _WARN_FRACTION or s["open"] - last >= _WARN_JUMP:
                log.warning("open fds %d/%d, threads %d · kinds %s · top sockets %s · top files %s",
                            s["open"], s["limit"], s["threads"], s["kinds"], s["top_sockets"], s["top_files"])
            last = s["open"]
        except Exception:
            log.exception("fd sample failed")


def start() -> None:
    threading.Thread(target=_loop, name="fdwatch", daemon=True).start()
