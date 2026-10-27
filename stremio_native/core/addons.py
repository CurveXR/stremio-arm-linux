"""Client for the Stremio addon protocol.

Spec: https://github.com/Stremio/stremio-addon-sdk/blob/master/docs/protocol.md
Every resource lives at {base}/{resource}/{type}/{id}[/{extra}].json
"""

from urllib.parse import quote

from .util import get_json

CINEMETA_URL = "https://v3-cinemeta.strem.io/manifest.json"
DEFAULT_ADDON_URLS = [
    CINEMETA_URL,
    "https://opensubtitles-v3.strem.io/manifest.json",
]


def encode_extra(extra):
    if not extra:
        return ""
    return "/" + "&".join(f"{quote(str(k), safe='')}={quote(str(v), safe='')}"
                          for k, v in extra.items() if v not in (None, ""))


class Addon:
    """An installed addon: a transport URL plus its manifest (as the account API stores it)."""

    def __init__(self, descriptor):
        self.descriptor = descriptor  # kept verbatim so syncing back is lossless
        self.transport_url = descriptor["transportUrl"]
        self.manifest = descriptor.get("manifest") or {}
        self.base = self.transport_url.rsplit("/manifest.json", 1)[0].rstrip("/")

    @classmethod
    def from_url(cls, url):
        url = url.strip()
        if url.startswith("stremio://"):
            url = "https://" + url[len("stremio://"):]
        manifest = get_json(url)
        if "id" not in manifest or "resources" not in manifest:
            raise ValueError("That URL doesn't serve a Stremio addon manifest")
        return cls({"transportUrl": url, "transportName": "http", "manifest": manifest, "flags": {}})

    @property
    def id(self):
        return self.manifest.get("id", self.transport_url)

    @property
    def name(self):
        return self.manifest.get("name", self.id)

    @property
    def protected(self):
        return bool((self.descriptor.get("flags") or {}).get("protected"))

    @property
    def catalogs(self):
        return self.manifest.get("catalogs") or []

    def supports(self, resource, type_, id_=None):
        for res in self.manifest.get("resources") or []:
            if isinstance(res, str):
                name, types, prefixes = res, self.manifest.get("types"), self.manifest.get("idPrefixes")
            else:
                name = res.get("name")
                types = res.get("types", self.manifest.get("types"))
                prefixes = res.get("idPrefixes", self.manifest.get("idPrefixes"))
            if name != resource:
                continue
            if types is not None and type_ not in types:
                continue
            if id_ is not None and prefixes and not any(id_.startswith(p) for p in prefixes):
                continue
            return True
        return False

    def _get(self, resource, type_, id_, extra=None):
        url = f"{self.base}/{resource}/{quote(type_, safe='')}/{quote(id_, safe='')}{encode_extra(extra)}.json"
        return get_json(url)

    def catalog(self, type_, catalog_id, **extra):
        return self._get("catalog", type_, catalog_id, extra).get("metas") or []

    def meta(self, type_, id_):
        return self._get("meta", type_, id_).get("meta")

    def streams(self, type_, id_):
        return self._get("stream", type_, id_).get("streams") or []

    def subtitles(self, type_, id_, **extra):
        return self._get("subtitles", type_, id_, extra).get("subtitles") or []

    def addon_catalog(self, type_, catalog_id):
        return self._get("addon_catalog", type_, catalog_id).get("addons") or []


def catalog_extras(catalog):
    """Return (names of all supported extras, names of required extras) for a catalog."""
    names, required = set(catalog.get("extraSupported") or []), set(catalog.get("extraRequired") or [])
    for e in catalog.get("extra") or []:
        names.add(e["name"])
        if e.get("isRequired"):
            required.add(e["name"])
    return names, required


def catalog_genres(catalog):
    for e in catalog.get("extra") or []:
        if e["name"] == "genre" and e.get("options"):
            return e["options"]
    return catalog.get("genres") or []


def catalog_title(addon, catalog):
    name = catalog.get("name") or catalog["id"].replace("_", " ").title()
    return f"{name} · {catalog['type'].title()}"
