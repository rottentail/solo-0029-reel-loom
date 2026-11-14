import json
from .errors import require, missing, revision_conflict
from .mixing import normalize_ducking
from .timebase import range_ticks, ticks, TICKS_PER_SECOND
from .values import identifier, text, now, canonical, digest, object_body, integer, number

TRACK_FIELDS = ("asset", "in", "out", "at", "gain_db", "label")
TRACK_NAMES = ("background", "narration")


def track_bounds(raw, label):
    """Validate a mix track placed on the reel timeline."""
    object_body(raw, TRACK_FIELDS, ("asset",))
    a = ticks(raw.get("in", 0), f"{label} in")
    b = ticks(raw["out"], f"{label} out") if "out" in raw else None
    at = ticks(raw.get("at", 0), f"{label} at")
    require(b is None or a < b, "empty_track", f"{label} must have positive length")
    gain = number(raw.get("gain_db", 0), f"{label} gain_db", -24, 12)
    require(at >= 0, "track_position", f"{label} cannot start before the reel")
    return {"asset": raw["asset"], "in_tick": a, "out_tick": b,
            "at_tick": at, "gain_db": gain,
            "label": text(raw["label"], f"{label} label") if "label" in raw else label}


class Reels:
    def __init__(self, db, assets, keys, settings):
        self.db, self.assets, self.keys, self.settings = db, assets, keys, settings

    def normalize(self, body, db):
        object_body(body, ("title", "slices", "tracks", "ducking"), ("title", "slices"))
        title = text(body["title"], "title")
        require(isinstance(body["slices"], list) and 1 <= len(body["slices"]) <= 64,
                "slice_count", "A reel requires 1..64 slices")
        slices, cursor = [], 0
        for raw in body["slices"]:
            object_body(raw, ("asset", "in", "out", "label"), ("asset", "in", "out"))
            asset = self.assets.get(raw["asset"], db, ready=True)
            a, b = range_ticks(raw["in"], raw["out"], asset["metadata"]["duration_tick"])
            slices.append({"id": identifier("slice"), "asset": asset["id"],
                           "in_tick": a, "out_tick": b, "at_tick": cursor,
                           "label": text(raw.get("label", asset["label"]), "slice label")})
            cursor += b-a
        require(cursor <= self.settings.max_seconds * TICKS_PER_SECOND,
                "reel_duration", "Reel exceeds service duration limit")
        tracks = self._tracks(body.get("tracks", {}), cursor, db)
        ducking = None
        if "narration" in tracks:
            ducking = normalize_ducking(body.get("ducking", {}))
        elif "ducking" in body:
            require(False, "mix_tracks", "Ducking settings require a narration track")
        return {"title": title, "slices": slices, "duration_tick": cursor,
                "tracks": tracks, "ducking": ducking}

    def _tracks(self, raw, duration, db):
        object_body(raw, TRACK_NAMES)
        tracks = {}
        for name in TRACK_NAMES:
            if name not in raw:
                continue
            track = track_bounds(raw[name], name)
            asset = self.assets.get(track["asset"], db, ready=True)
            require(asset["metadata"]["has_audio"], "audio_required",
                    f"{name} track source has no audio stream")
            out = track["out_tick"]
            if out is None:
                out = track["in_tick"] + (duration - track["at_tick"])
            require(out <= asset["metadata"]["duration_tick"], "track_bounds",
                    f"{name} track extends beyond its source media",
                    available=asset["metadata"]["duration_tick"])
            require(track["at_tick"] < duration, "track_bounds",
                    f"{name} track starts after the reel ends")
            # The mix is trimmed to the reel, so a bed may run past the last cut
            # but must never be scheduled to begin after it.
            tracks[name] = {"id": identifier("track"), "asset": asset["id"],
                            "in_tick": track["in_tick"], "out_tick": out,
                            "at_tick": track["at_tick"], "gain_db": track["gain_db"],
                            "label": track["label"]}
        if "narration" in tracks:
            require("background" in tracks, "mix_tracks",
                    "A narration track requires a background track")
        return tracks

    def create(self, body, key=None):
        with self.db.write_unit() as db:
            previous = self.keys.replay(db, "reels", key, body)
            if previous:
                return previous
            document = self.normalize(body, db)
            reel_id = identifier("reel")
            db.execute("INSERT INTO reels VALUES(?,?,?,?,?)", (reel_id, document["title"], 1, "open", now()))
            self._write_revision(db, reel_id, 1, document)
            result = self.get(reel_id, db=db)
            return self.keys.record(db, "reels", key, body, result)

    def _write_revision(self, db, reel_id, revision, document):
        db.execute("INSERT INTO revisions VALUES(?,?,?,?,?)",
                   (reel_id, revision, canonical(document), digest(document), now()))
        self.db.event(db, reel_id, "revision_written", {"revision": revision, "hash": digest(document)})

    def replace(self, reel_id, expected, body):
        integer(expected, "expected_revision", 1)
        with self.db.write_unit() as db:
            current = self.get(reel_id, db=db)
            require(current["state"] == "open", "reel_sealed", "Sealed reels cannot be edited", 409)
            if current["head"] != expected:
                raise revision_conflict(expected, current["head"])
            document = self.normalize(body, db)
            revision = expected + 1
            self._write_revision(db, reel_id, revision, document)
            db.execute("UPDATE reels SET head=?,title=? WHERE id=?", (revision, document["title"], reel_id))
            return self.get(reel_id, db=db)

    def get(self, reel_id, revision=None, db=None):
        if db is None:
            with self.db.read() as read:
                return self.get(reel_id, revision, read)
        row = db.execute("SELECT * FROM reels WHERE id=?", (reel_id,)).fetchone()
        if row is None:
            raise missing("reel", reel_id)
        target = row["head"] if revision is None else integer(revision, "revision", 1)
        rev = db.execute("SELECT * FROM revisions WHERE reel=? AND number=?", (reel_id, target)).fetchone()
        if rev is None:
            raise missing("revision", target)
        return {**dict(row), "revision": target, "document": json.loads(rev["document"]), "hash": rev["hash"]}

    def list(self):
        with self.db.read() as db:
            return [self.get(row["id"], db=db) for row in db.execute("SELECT id FROM reels ORDER BY created DESC LIMIT 100")]

    def seal(self, reel_id, expected):
        with self.db.write_unit() as db:
            reel = self.get(reel_id, db=db)
            if reel["head"] != expected:
                raise revision_conflict(expected, reel["head"])
            db.execute("UPDATE reels SET state='sealed' WHERE id=?", (reel_id,))
            self.db.event(db, reel_id, "sealed", {"revision": expected})
            return self.get(reel_id, db=db)

    def history(self, reel_id):
        with self.db.read() as db:
            self.get(reel_id, db=db)
            rows = db.execute("SELECT number,hash,created,document FROM revisions WHERE reel=? ORDER BY number DESC LIMIT 100", (reel_id,))
            result = []
            for row in rows:
                document = json.loads(row["document"])
                result.append({"revision": row["number"], "hash": row["hash"], "created": row["created"],
                               "title": document["title"], "slices": len(document["slices"]),
                               "duration_tick": document["duration_tick"]})
            return result
