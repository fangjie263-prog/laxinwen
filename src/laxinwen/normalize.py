"""URL canonicalization and title fingerprinting helpers."""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query params that are pure tracking noise and should be stripped.
_TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "fbclid",
    "gclid",
    "dclid",
    "igshid",
    "mc_cid",
    "mc_eid",
    "ref",
    "ref_src",
    "ref_url",
    "spm",
    "from",
}

_WHITESPACE_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[\W_]+", re.UNICODE)


def canonicalize_url(url: str, *, keep_tracking: bool = False) -> str:
    """Normalize a URL so equal pages map to the same canonical string.

    Handles: scheme/host lowercasing, default-port removal, fragment removal,
    and stripping of common tracking params. The fragment is dropped; query
    params are kept (sorted) so different feeds of the same article still
    collide on their path.
    """
    if not url:
        return url
    url = url.strip()
    parts = urlsplit(url)

    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    # Normalize international host names to punycode.
    host = host.encode("idna").decode("ascii") if host else ""

    port = parts.port
    if port and not (
        (scheme == "http" and port == 80)
        or (scheme == "https" and port == 443)
    ):
        netloc = f"{host}:{port}"
    else:
        netloc = host

    path = parts.path or "/"
    # Collapse duplicate slashes in path (keep scheme's "//").
    path = re.sub(r"/{2,}", "/", path)
    # Normalize trailing slash? Keep it as-is to avoid losing info.

    query = parts.query
    if not keep_tracking:
        params = [
            (k, v)
            for k, v in parse_qsl(query, keep_blank_values=True)
            if k.lower() not in _TRACKING_PARAMS
        ]
        query = urlencode(sorted(params))

    return urlunsplit((scheme, netloc, path, query, ""))


def title_fingerprint(title: str, *, strip_site_suffix: str | None = None) -> str:
    """Compute a stable, normalized fingerprint for a title.

    Pipeline: Unicode NFKC → strip site suffix → lowercase → collapse
    whitespace → remove punctuation → return the cleaned string itself so it
    remains human-readable while being deterministic.
    """
    if not title:
        return ""
    t = unicodedata.normalize("NFKC", title)
    if strip_site_suffix:
        suffix = strip_site_suffix.strip()
        if suffix and t.rstrip().lower().endswith(suffix.lower()):
            t = t[: -len(suffix)].rstrip()
    t = t.lower()
    t = _WHITESPACE_RE.sub(" ", t)
    t = _PUNCT_RE.sub("", t)
    return t.strip()
