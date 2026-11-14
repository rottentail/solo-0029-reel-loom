import hashlib
import json
import zipfile
from .errors import require, missing
from .mixing import inspect_control
from .values import identifier, now, canonical, digest, object_body


class Imprints:
    def __init__(self, db, blobs, tickets, captions):
        self.db, self.blobs, self.tickets, self.captions = db, blobs, tickets, captions

    def create(self, ticket_id, raw):
        object_body(raw, ("cue_tracks",))
        track_ids = raw.get("cue_tracks", [])
        require(isinstance(track_ids, list) and len(track_ids) <= 64 and all(isinstance(x,str) for x in track_ids),
                "invalid_tracks", "cue_tracks must be an array of identifiers")
        ticket = self.tickets.get(ticket_id)
        require(ticket["state"] == "succeeded", "imprint_state", "Only completed renditions can be packaged", 409)
        caption = self.captions.project(ticket["reel"], ticket["revision"], track_ids) if track_ids else None
        files, mix = [], None
        for item in ticket["artifacts"]:
            if item["kind"] == "ducking_control":
                mix = item
                continue
            if item["kind"] != "rendition":
                continue
            extension = "mp4" if ticket["snapshot"]["look"]["spec"]["kind"] == "video" else "wav"
            blob = self.blobs.get(item["blob"])
            files.append({"name": f'{item["id"]}.{extension}', "blob": item["blob"], "size": blob["size"], "kind": item["kind"]})
        require(files, "missing_rendition", "A completed render requires a rendition artifact", 409)
        extras = []
        if mix is not None:
            fresh = self.blobs.get(mix["blob"])
            control = {"name": "ducking-control.wav", "blob": mix["blob"], "size": fresh["size"],
                       "kind": mix["kind"], "metadata": mix["metadata"]}
            control["digest"] = inspect_control(self.blobs.resolve(mix["blob"], verify=True).read_bytes())
            require(control["digest"]["sha256"] == control["metadata"]["envelope"]["sha256"],
                    "mix_record_mismatch", "Frozen gain control differs from the mix record", 409)
            extras.append(control)
        manifest = {"schema": 1, "ticket": ticket_id, "fingerprint": ticket["fingerprint"],
                    "snapshot": ticket["snapshot"], "files": files, "caption": caption,
                    "mix": next((item["metadata"].get("mix") for item in ticket["artifacts"]
                                 if item["kind"] == "rendition"), None),
                    "extras": extras}
        fingerprint = digest(manifest)
        with self.db.read() as db:
            row = db.execute("SELECT id FROM imprints WHERE fingerprint=?", (fingerprint,)).fetchone()
            if row:
                return self.get(row["id"], db)
        with self.blobs.temporary(".zip") as package:
            with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_STORED) as archive:
                self._write(archive, "manifest.json", canonical(manifest).encode())
                if caption:
                    self._write(archive, "captions.srt", caption["srt"].encode())
                for item in files + extras:
                    info = zipfile.ZipInfo(item["name"], date_time=(1980,1,1,0,0,0))
                    info.external_attr = 0o644 << 16
                    with archive.open(info, "w") as target, self.blobs.resolve(item["blob"], verify=True).open("rb") as source:
                        for chunk in iter(lambda: source.read(1024*1024), b""):
                            target.write(chunk)
            with self.db.write_unit() as db:
                row = db.execute("SELECT id FROM imprints WHERE fingerprint=?", (fingerprint,)).fetchone()
                if row:
                    return self.get(row["id"], db)
                blob = self.blobs.put(package, db)
                imprint_id = identifier("imprint")
                db.execute("INSERT INTO imprints VALUES(?,?,?,?,?,?)",
                           (imprint_id, ticket_id, canonical(manifest), blob["hash"], fingerprint, now()))
                self.db.event(db, ticket_id, "imprinted", {"imprint": imprint_id, "blob": blob["hash"]})
                return self.get(imprint_id, db)

    def _write(self, archive, name, content):
        info = zipfile.ZipInfo(name, date_time=(1980,1,1,0,0,0))
        info.external_attr = 0o644 << 16
        archive.writestr(info, content)

    def get(self, imprint_id, db=None):
        if db is None:
            with self.db.read() as read:
                return self.get(imprint_id, read)
        row = db.execute("SELECT * FROM imprints WHERE id=?", (imprint_id,)).fetchone()
        if row is None:
            raise missing("imprint", imprint_id)
        return {**dict(row), "manifest": json.loads(row["manifest"])}

    def verify(self, imprint_id):
        imprint = self.get(imprint_id)
        path = self.blobs.resolve(imprint["blob"], verify=True)
        manifest = imprint["manifest"]
        with zipfile.ZipFile(path) as archive:
            require(json.loads(archive.read("manifest.json")) == manifest,
                    "manifest_mismatch", "Packaged manifest differs from frozen record", 409)
            expected_names = {"manifest.json"} | {item["name"] for item in manifest["files"] + manifest.get("extras", [])}
            if manifest["caption"]:
                expected_names.add("captions.srt")
                require(archive.read("captions.srt").decode() == manifest["caption"]["srt"],
                        "caption_mismatch", "Packaged captions differ from frozen projection", 409)
            require(set(archive.namelist()) == expected_names, "package_members", "Unexpected package members", 409)
            verified, controls = [], []
            for item in manifest["files"] + manifest.get("extras", []):
                hasher, size = hashlib.sha256(), 0
                with archive.open(item["name"]) as content:
                    for chunk in iter(lambda: content.read(1024*1024), b""):
                        hasher.update(chunk)
                        size += len(chunk)
                require(hasher.hexdigest() == item["blob"] and size == item["size"],
                        "rendition_mismatch", "Packaged media differs from frozen content", 409)
                if item["kind"] == "ducking_control":
                    actual = inspect_control(archive.read(item["name"]))
                    require(actual == item.get("digest") and
                            actual["sha256"] == item["metadata"]["envelope"]["sha256"],
                            "mix_record_mismatch", "Packaged gain control differs from the frozen mix record", 409)
                    controls.append({"name": item["name"], **actual})
                verified.append(item["name"])
        return {"id": imprint_id, "valid": True, "verified_files": verified,
                "controls": controls,
                "fingerprint": imprint["fingerprint"], "blob": imprint["blob"]}
