"""Turn an addon stream object into something a player can open."""

from dataclasses import dataclass
from urllib.parse import quote

from . import torrent
from .util import post_json

_engine = None


def engine():
    global _engine
    if _engine is None and torrent.available():
        _engine = torrent.TorrentEngine()
    return _engine


def shutdown():
    if _engine:
        _engine.shutdown()


@dataclass
class Playable:
    url: str
    kind: str                 # "http" | "torrent" | "external"
    info_hash: str = None
    via: str = ""             # human-readable description of where it plays from


def describe(stream):
    if stream.get("url"):
        return "Direct link"
    if stream.get("infoHash"):
        return "Torrent"
    if stream.get("ytId"):
        return "YouTube"
    if stream.get("externalUrl"):
        return "Opens externally"
    return "Unknown"


def resolve(stream, settings):
    """Blocking – call from a worker thread."""
    if stream.get("url", "").startswith("file:"):
        return Playable(stream["url"], "http", via="your drive")
    if stream.get("url"):
        return Playable(stream["url"], "http", via="direct")
    if stream.get("ytId"):
        return Playable(f"https://www.youtube.com/watch?v={stream['ytId']}", "external", via="YouTube")
    if stream.get("externalUrl"):
        return Playable(stream["externalUrl"], "external", via="external")
    ih = stream.get("infoHash")
    if not ih:
        raise ValueError("This stream has no playable source")
    ih = ih.lower()
    sources = stream.get("sources") or []
    mode = settings.get("torrent_engine", "auto")
    eng = engine() if mode in ("auto", "builtin") else None
    if eng:
        return Playable(eng.stream_url(ih, stream.get("fileIdx"), sources), "torrent", ih, via="built-in engine")
    if mode == "builtin":
        raise RuntimeError("The built-in torrent engine needs libtorrent (sudo apt install python3-libtorrent)")
    return Playable(_server_url(settings["server_url"].rstrip("/"), ih, stream.get("fileIdx"), sources),
                    "torrent", ih, via="streaming server")


def _server_url(server, ih, file_idx, sources):
    """Use a Stremio streaming server (the one bundled with official Stremio, port 11470)."""
    if file_idx is None:
        body = {"torrent": {"infoHash": ih}, "guessFileIdx": {}}
        if sources:
            body["peerSearch"] = {"sources": sources + [f"dht:{ih}"], "min": 40, "max": 150}
        try:
            file_idx = post_json(f"{server}/{ih}/create", body, timeout=60).get("guessedFileIdx", 0)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"Streaming server at {server} is not reachable ({e})") from e
    trackers = "&".join("tr=" + quote(s, safe="") for s in sources if s.startswith("tracker:"))
    return f"{server}/{ih}/{file_idx}" + (f"?{trackers}" if trackers else "")
