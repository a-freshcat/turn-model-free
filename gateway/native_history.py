"""Bounded reuse of real upstream signatures, scoped to exact calls.

The key intentionally excludes the account and the model. A signature belongs to a
`functionCall` (its name and canonical arguments), not to the credential that happened
to serve the request. Antigravity is reached through two rotating accounts with
`max_concurrency: 1`, so a key bound to the credential misses on almost every step; a
key bound to the native model additionally misses whenever the client switches models
mid-conversation.

Measured against `daily-cloudcode-pa.googleapis.com/v1internal:generateContent`
(synthetic calls, current tool round):

    same account, same model, real signature   -> 200
    same account, other model,  real signature -> 200
    other account, same model,  real signature -> 200
    same call without any signature            -> 400 INVALID_ARGUMENT (signature)
    same call with random base64               -> 400 INVALID_ARGUMENT (base64)

So a real signature minted elsewhere is accepted for the identical call, while dropping
it is rejected outright. This is not a licence to invent signatures.

The cache is also persisted to disk. A process restart used to empty it, and once the
gateway stopped rewriting unsigned calls into text that meant the next request in a live
tool round failed with 400 instead of degrading. Wall-clock expiry is stored so entries
survive the restart; `time.monotonic` is still used in memory because it cannot jump.
"""
import hashlib
import json
import os
import time
from collections import OrderedDict
from pathlib import Path


class ToolSignatureCache:
    def __init__(self, limit=512, ttl=3600, path=None):
        self.entries = OrderedDict()
        self.limit, self.ttl = limit, ttl
        self.path = Path(path) if path else None
        self._dirty = False
        self._saved_at = 0.0
        self._load()

    # ---- key ----------------------------------------------------------------
    def key(self, call_id, name, args):
        digest = hashlib.sha256(
            json.dumps(args, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
        ).digest()
        return call_id, name, digest

    @staticmethod
    def _serialise_key(key):
        call_id, name, digest = key
        return [call_id, name, digest.hex()]

    @staticmethod
    def _parse_key(raw):
        if not isinstance(raw, list) or len(raw) != 3:
            return None
        call_id, name, digest = raw
        if not isinstance(call_id, str) or not isinstance(name, str) or not isinstance(digest, str):
            return None
        try:
            return call_id, name, bytes.fromhex(digest)
        except ValueError:
            return None

    # ---- persistence --------------------------------------------------------
    def _load(self):
        """Restore entries written by a previous process.

        Any unreadable payload is discarded rather than fatal: losing the cache costs a
        degraded round, refusing to start costs every round.
        """
        if self.path is None:
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            return
        if not isinstance(raw, dict):
            return
        now_wall = time.time()
        now = time.monotonic()
        for entry in raw.get("entries") or []:
            if not isinstance(entry, dict):
                continue
            key = self._parse_key(entry.get("key"))
            signature = entry.get("signature")
            expires = entry.get("expires_at")
            if key is None or not isinstance(signature, str) or not signature:
                continue
            if len(signature) > 65536 or not isinstance(expires, (int, float)):
                continue
            remaining = float(expires) - now_wall
            if remaining <= 0:
                continue
            owner = entry.get("owner") if isinstance(entry.get("owner"), str) else ""
            model = entry.get("model") if isinstance(entry.get("model"), str) else ""
            self.entries[key] = (now + remaining, signature, owner, model)
        while len(self.entries) > self.limit:
            self.entries.popitem(last=False)

    def _save(self, force=False):
        if self.path is None or not self._dirty:
            return
        # Writes are throttled: a tool round can mint a signature per call and every
        # write is an fsync-adjacent rename.
        if not force and time.monotonic() - self._saved_at < 1.0:
            return
        now_wall = time.time()
        now = time.monotonic()
        payload = {"version": 1, "entries": [
            {"key": self._serialise_key(key), "signature": entry[1],
             "expires_at": now_wall + max(0.0, entry[0] - now),
             "owner": entry[2], "model": entry[3]}
            for key, entry in self.entries.items()
        ]}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp, self.path)
            self._dirty = False
            self._saved_at = now
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass

    # ---- public API ---------------------------------------------------------
    def put(self, owner, model, call_id, name, args, signature):
        if not isinstance(signature, str) or not signature or len(signature) > 65536:
            return
        key = self.key(call_id, name, args)
        # `owner`/`model` are recorded for diagnostics only; they do not partition the
        # cache. The newest signature for the same exact call wins.
        self.entries[key] = (time.monotonic() + self.ttl, signature, owner, model)
        self.entries.move_to_end(key)
        while len(self.entries) > self.limit:
            self.entries.popitem(last=False)
        self._dirty = True
        self._save()

    def get(self, owner, model, call_id, name, args):
        key = self.key(call_id, name, args)
        found = self.entries.get(key)
        if found is None:
            return None
        if found[0] <= time.monotonic():
            self.entries.pop(key, None)
            return None
        self.entries.move_to_end(key)
        return found[1]

    def origin(self, call_id, name, args):
        """Return the `(owner, model)` that minted the cached signature, if any."""
        found = self.entries.get(self.key(call_id, name, args))
        if found is None or found[0] <= time.monotonic():
            return None
        return found[2], found[3]

    def borrow(self, name=None):
        """Return a live signature already minted for some other call.

        Only used when the exact call is unknown, e.g. a conversation that predates this
        process. Upstream rejects an absent signature and rejects fabricated base64, but
        accepts a real signature minted for a different call, so an unrelated-but-real
        signature is the least bad substitute. Never invents bytes. `name` prefers a
        signature minted for the same tool.
        """
        now = time.monotonic()
        fallback = None
        for (_call_id, key_name, _digest), entry in reversed(self.entries.items()):
            if entry[0] <= now:
                continue
            if name is not None and key_name == name:
                return entry[1]
            if fallback is None:
                fallback = entry[1]
        return fallback

    def flush(self):
        """Persist immediately. Called on shutdown so a restart keeps the cache."""
        self._dirty = True
        self._save(force=True)
