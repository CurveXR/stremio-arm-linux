"""Shared helpers: HTTP session, background work, XDG paths."""

import os
import threading
from concurrent.futures import ThreadPoolExecutor

import requests
from gi.repository import GLib

APP_ID = os.environ.get("STREMIO_NATIVE_APP_ID", "io.github.stremio_native")  # override only for tests
USER_AGENT = "StremioNative/0.1 (+GTK4)"

_executor = ThreadPoolExecutor(max_workers=12, thread_name_prefix="worker")
_local = threading.local()


def session():
    """One requests.Session per worker thread (Session isn't thread-safe)."""
    s = getattr(_local, "session", None)
    if s is None:
        s = requests.Session()
        s.headers["User-Agent"] = USER_AGENT
        _local.session = s
    return s


def get_json(url, timeout=20):
    r = session().get(url, timeout=timeout)
    r.raise_for_status()
    return r.json()


def post_json(url, payload, timeout=20):
    r = session().post(url, json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()


def run_async(fn, *args, on_done=None, on_error=None):
    """Run fn(*args) on a worker thread; deliver the result on the GTK main loop."""

    def job():
        try:
            result = fn(*args)
        except Exception as e:  # noqa: BLE001 - surfaced to the UI
            if on_error:
                GLib.idle_add(_call, on_error, e)
            else:
                print(f"[async] {fn.__name__} failed: {e!r}")
            return
        if on_done:
            GLib.idle_add(_call, on_done, result)

    return _executor.submit(job)


def _call(cb, value):
    cb(value)
    return GLib.SOURCE_REMOVE


def _xdg(var, fallback):
    base = os.environ.get(var) or os.path.expanduser(fallback)
    path = os.path.join(base, "stremio-native")
    os.makedirs(path, exist_ok=True)
    return path


def config_dir():
    return _xdg("XDG_CONFIG_HOME", "~/.config")


def cache_dir(*sub):
    path = os.path.join(_xdg("XDG_CACHE_HOME", "~/.cache"), *sub)
    os.makedirs(path, exist_ok=True)
    return path
