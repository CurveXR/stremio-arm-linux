"""Stremio account API (api.strem.io) – login, addon collection and library sync."""

from datetime import datetime, timezone

from .util import post_json

API = "https://api.strem.io/api/"


class ApiError(Exception):
    pass


def _call(method, payload):
    data = post_json(API + method, payload)
    if data.get("error"):
        err = data["error"]
        raise ApiError(err.get("message") if isinstance(err, dict) else str(err))
    return data.get("result")


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def login(email, password):
    """Returns (auth_key, user dict)."""
    res = _call("login", {"type": "Login", "email": email, "password": password, "facebook": False})
    return res["authKey"], res.get("user") or {}


def logout(auth_key):
    try:
        _call("logout", {"type": "Logout", "authKey": auth_key})
    except Exception:  # noqa: BLE001 - logging out locally is what matters
        pass


def get_addons(auth_key):
    res = _call("addonCollectionGet", {"type": "AddonCollectionGet", "authKey": auth_key, "update": True})
    return res.get("addons") or []


def set_addons(auth_key, descriptors):
    _call("addonCollectionSet", {"type": "AddonCollectionSet", "authKey": auth_key, "addons": descriptors})


def get_library(auth_key):
    res = _call("datastoreGet", {"authKey": auth_key, "collection": "libraryItem", "ids": [], "all": True})
    return res or []


def put_library(auth_key, items):
    _call("datastorePut", {"authKey": auth_key, "collection": "libraryItem", "changes": items})


def new_library_item(meta):
    """Build a libraryItem in the shape the official clients write."""
    ts = now_iso()
    return {
        "_id": meta["id"],
        "name": meta.get("name", ""),
        "type": meta.get("type", "movie"),
        "poster": meta.get("poster"),
        "posterShape": meta.get("posterShape", "poster"),
        "removed": False,
        "temp": False,
        "_ctime": ts,
        "_mtime": ts,
        "state": {
            "lastWatched": None, "timeWatched": 0, "timeOffset": 0, "overallTimeWatched": 0,
            "timesWatched": 0, "flaggedWatched": 0, "duration": 0, "video_id": None,
            "watched": None, "noNotif": False,
        },
        "behaviorHints": {"defaultVideoId": None, "featuredVideoId": None, "hasScheduledVideos": False},
    }
