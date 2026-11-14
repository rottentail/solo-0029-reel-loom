"""Background ducking built from the narration programme placed on a reel.

Ducking is a reel-level property: the bed and the narration are each placed
once on the edit timeline, so a bed crossing several hard cuts is lowered by a
single continuous envelope instead of a per-slice pump.  Source-time silence
windows are only an intermediate fact; they are projected through the narration
placement before they influence a mix.
"""
import hashlib
import io
import json
import sys
from array import array
import wave

from .errors import require
from .probe import select_stream
from .samples import read_pcm, silence_intervals
from .timebase import TICKS_PER_SECOND
from .values import now, number, object_body, canonical, digest

ENVELOPE_RATE = 1000
ANALYSIS_RATE = 16000
ALGORITHM = "silence-duck/1"

DUCKING_FIELDS = ("depth_db", "threshold_db", "minimum_silence", "attack", "hold", "release")
DUCKING_RANGES = {
    "depth_db": (-40.0, 0.0),
    "threshold_db": (-100.0, -1.0),
    "minimum_silence": (0.01, 10.0),
    "attack": (0.01, 2.0),
    "hold": (0.0, 2.0),
    "release": (0.05, 3.0),
}
DEFAULT_DUCKING = {
    # 14 dB of bed reduction with a gentle 350 ms return between phrases.
    "depth_db": -14.0,
    "threshold_db": -40.0,
    "minimum_silence": 0.15,
    "attack": 0.08,
    "hold": 0.15,
    "release": 0.35,
}


def normalize_ducking(raw, base=None):
    """Validate ducking controls, layering explicit fields over a base/default."""
    object_body(raw, DUCKING_FIELDS)
    merged = dict(DEFAULT_DUCKING if base is None else base)
    for key in DUCKING_FIELDS:
        if key in raw:
            low, high = DUCKING_RANGES[key]
            merged[key] = number(raw[key], key, low, high)
    for key in DUCKING_FIELDS:
        require(key in merged, "ducking_settings", "Ducking settings are incomplete")
    return {key: float(merged[key]) for key in DUCKING_FIELDS}


def speech_windows(silence, duration_tick):
    """Complement of source silence, expressed in source ticks."""
    duration_tick = max(0, int(duration_tick))
    windows, cursor = [], 0
    for gap in sorted(silence, key=lambda item: (item["start_tick"], item["end_tick"])):
        start = max(cursor, min(duration_tick, int(gap["start_tick"])))
        end = max(start, min(duration_tick, int(gap["end_tick"])))
        if start > cursor:
            windows.append({"start_tick": cursor, "end_tick": start})
        cursor = max(cursor, end)
    if cursor < duration_tick:
        windows.append({"start_tick": cursor, "end_tick": duration_tick})
    return [item for item in windows if item["end_tick"] > item["start_tick"]]


def project_speech(windows, track, reel_duration_tick):
    """Map narration source windows through in/out/at into reel time.

    Slice boundaries deliberately do not participate: narration placement is a
    reel-level track, so a phrase beginning near a cut stays one phrase.
    """
    result = []
    for item in windows:
        start = max(int(item["start_tick"]), track["in_tick"])
        end = min(int(item["end_tick"]), track["out_tick"])
        if start >= end:
            continue
        mapped_start = max(0, track["at_tick"] + (start - track["in_tick"]))
        mapped_end = min(int(reel_duration_tick), track["at_tick"] + (end - track["in_tick"]))
        if mapped_start < mapped_end:
            result.append({"start_tick": mapped_start, "end_tick": mapped_end})
    result.sort(key=lambda item: (item["start_tick"], item["end_tick"]))
    return result


def envelope_regions(windows, settings, duration_tick):
    """Merge speech windows into continuous low-gain regions.

    Every window is expanded by its attack and hold; regions closer than one
    release are merged, so a natural pause never pumps the bed back up only to
    drop it again.  The returned spans are the flat low-gain spans.
    """
    duration_tick = max(0, int(duration_tick))
    attack = round(settings["attack"] * TICKS_PER_SECOND)
    hold = round(settings["hold"] * TICKS_PER_SECOND)
    release = round(settings["release"] * TICKS_PER_SECOND)
    expanded = []
    for item in windows:
        start = max(0, int(item["start_tick"]) - attack)
        end = min(duration_tick, int(item["end_tick"]) + hold)
        if start < end:
            expanded.append([start, end])
    expanded.sort()
    merged = []
    for start, end in expanded:
        if merged and start <= merged[-1][1] + release:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [{"start_tick": start, "end_tick": end} for start, end in merged]


def envelope_samples(regions, settings, duration_tick, rate=ENVELOPE_RATE):
    """Sample the ducking gain envelope as mono 16-bit values.

    1 kHz is deliberate: the envelope stays cheap for long reels and FFmpeg
    interpolates it to the look sample rate when the mix is encoded.
    """
    total = _sample_index(int(duration_tick), rate)
    floor = 10 ** (settings["depth_db"] / 20)
    attack = round(settings["attack"] * TICKS_PER_SECOND)
    release = round(settings["release"] * TICKS_PER_SECOND)
    values = array("h", [32767]) * max(total, 0)
    for region in regions:
        start, end = int(region["start_tick"]), int(region["end_tick"])
        begin = max(0, _sample_index(start, rate))
        finish = min(total, _sample_index(end, rate))
        ramp = min(attack, max(1, end - start))
        attack_end = min(finish, _sample_index(start + ramp, rate))
        for index in range(begin, attack_end):
            tick = _sample_tick(index, rate)
            values[index] = _quantize(1 - (1 - floor) * (tick - start) / max(1, ramp))
        for index in range(attack_end, finish):
            values[index] = _quantize(floor)
        release_end = min(total, _sample_index(end + release, rate))
        for index in range(finish, release_end):
            tick = _sample_tick(index, rate)
            values[index] = _quantize(floor + (1 - floor) * (tick - end) / max(1, release))
    return values


def envelope_digest(values):
    """Digest of the mono envelope samples as little-endian 16-bit PCM."""
    copy = array("h", values)
    if sys.byteorder != "little":
        copy.byteswap()
    return hashlib.sha256(copy.tobytes()).hexdigest()


def inspect_control(data):
    """Read a delivered gain control back as mono envelope samples."""
    with wave.open(io.BytesIO(data), "rb") as source:
        require(source.getsampwidth() == 2, "control_format", "Gain control must be 16-bit PCM")
        channels = source.getnchannels()
        rate = source.getframerate()
        values = array("h")
        values.frombytes(source.readframes(source.getnframes()))
    if sys.byteorder != "little":
        values.byteswap()
    if channels == 2:
        values = array("h", values[::2])
    return {"rate": rate, "channels": channels, "samples": len(values),
            "sha256": envelope_digest(values)}


def write_control(path, values, channels):
    """Write the gain envelope as an interleaved PCM WAV at ENVELOPE_RATE."""
    channels = 1 if channels == 1 else 2
    if channels == 2:
        data = array("h")
        for value in values:
            data.extend((value, value))
    else:
        data = array("h", values)
    if sys.byteorder != "little":
        data.byteswap()
    with wave.open(str(path), "wb") as target:
        target.setnchannels(channels)
        target.setsampwidth(2)
        target.setframerate(ENVELOPE_RATE)
        target.writeframes(data.tobytes())
    return path


def delay_samples(at_tick, sample_rate):
    return int(round(int(at_tick) * sample_rate / TICKS_PER_SECOND))


def _sample_index(tick, rate):
    return (int(tick) * rate + TICKS_PER_SECOND // 2) // TICKS_PER_SECOND


def _sample_tick(index, rate):
    return (int(index) * TICKS_PER_SECOND) // rate


def _quantize(gain):
    return max(0, min(32767, int(round(gain * 32767))))


class Mixing:
    """Audition previews and render-time envelope preparation for a snapshot."""

    def __init__(self, db, assets, blobs, reels, analysis, processes):
        self.db, self.assets, self.blobs = db, assets, blobs
        self.reels, self.analysis, self.processes = reels, analysis, processes

    # -- shared facts -----------------------------------------------------
    def track_reports(self, tracks, sources):
        reports = {}
        for name, track in tracks.items():
            source = sources[track["asset"]]
            audio = select_stream(source["metadata"], "audio")
            require(audio is not None, "audio_required", "A mix track requires an audio stream")
            reports[name] = {"asset": track["asset"], "blob": source["blob"],
                             "in_tick": track["in_tick"], "out_tick": track["out_tick"],
                             "at_tick": track["at_tick"], "gain_db": track["gain_db"],
                             "source_sample_rate": audio["sample_rate"],
                             "source_channels": audio["channels"]}
        return reports

    def _record(self, identity, duration_tick, tracks, sources, settings, speech, windows, regions):
        values = None
        envelope = None
        if settings is not None:
            values = envelope_samples(regions, settings, duration_tick)
            envelope = {"algorithm": ALGORITHM, "rate": ENVELOPE_RATE,
                        "samples": len(values), "sha256": envelope_digest(values),
                        "floor_db": settings["depth_db"]}
        record = {"schema": 1, "algorithm": ALGORITHM, **identity,
                  "duration_tick": int(duration_tick),
                  "tracks": self.track_reports(tracks, sources),
                  "ducking": dict(settings) if settings is not None else None,
                  "narration_speech": list(speech),
                  "windows": windows, "regions": regions, "envelope": envelope}
        return record, values

    def _speech_from(self, path, settings, gain_db=0.0):
        """Speech windows from the narration, judged at the level it is heard.

        Track gain shifts every level by a constant, so shifting the silence
        threshold by the inverse amount is numerically identical to scaling the
        samples - and keeps the render in exact agreement with audition.
        """
        samples = read_pcm(path)
        require(len(samples) > 0, "empty_audio", "Narration audio decoded to no samples")
        end_tick = round(len(samples) * TICKS_PER_SECOND / ANALYSIS_RATE)
        threshold = max(-100.0, min(-1.0, settings["threshold_db"] - gain_db))
        silence = silence_intervals(samples, ANALYSIS_RATE, threshold, settings["minimum_silence"])
        return speech_windows(silence, end_tick)

    def _programme_key(self, narration_blob, track, settings, duration_tick):
        """Stable identity of a ducking programme.

        The narration bytes, the reel placement of that narration, the ducking
        controls and the reel length define the programme.  Placement is part of
        the key on purpose: the same recording edited to start elsewhere must
        not inherit another cut's reel-time schedule.
        """
        return digest({"algorithm": ALGORITHM, "rate": ENVELOPE_RATE,
                       "narration": narration_blob,
                       "placement": {"in_tick": track["in_tick"], "out_tick": track["out_tick"],
                                     "at_tick": track["at_tick"], "gain_db": track["gain_db"]},
                       "ducking": settings, "duration_tick": int(duration_tick)})

    def _load_plan(self, key, db):
        row = db.execute("SELECT plan FROM mix_plans WHERE fingerprint=?", (key,)).fetchone()
        return json.loads(row["plan"]) if row else None

    def _store_plan(self, key, plan):
        with self.db.write_unit() as db:
            db.execute("INSERT OR IGNORE INTO mix_plans VALUES(?,?,?)", (key, canonical(plan), now()))
            self.db.event(db, key, "mix_plan_stored", {"algorithm": ALGORITHM})
            return self._load_plan(key, db)

    # -- render path ------------------------------------------------------
    def prepare(self, snapshot, resolve, control_path):
        """Build the mix record and, when ducking applies, the gain-control WAV.

        The envelope is derived from the narration bytes in the frozen snapshot,
        so it follows the cut that the ticket froze rather than the current head.
        """
        document = snapshot["document"]
        tracks = document.get("tracks")
        if not tracks:
            return None
        settings = document.get("ducking")
        identity = {"reel": snapshot["reel"], "revision": snapshot["revision"],
                    "reel_hash": snapshot["reel_hash"]}
        if settings is None:
            record, _ = self._record(identity, document["duration_tick"], tracks,
                                     snapshot["sources"], None, [], [], [])
            return {"control": None, "report": record}
        narration = tracks["narration"]
        blob = snapshot["sources"][narration["asset"]]["blob"]
        duration = document["duration_tick"]
        key = self._programme_key(blob, narration, settings, duration)
        with self.db.read() as db:
            plan = self._load_plan(key, db)
        if plan is None:
            with self.blobs.temporary(".pcm") as pcm:
                self.processes.decode_audio(resolve(blob), pcm, ANALYSIS_RATE)
                speech = self._speech_from(pcm, settings, narration["gain_db"])
            windows = project_speech(speech, narration, duration)
            plan = {"narration_speech": speech, "windows": windows,
                    "regions": envelope_regions(windows, settings, duration)}
            plan = self._store_plan(key, plan)
        record, values = self._record(identity, duration, tracks, snapshot["sources"],
                                      settings, plan["narration_speech"],
                                      plan["windows"], plan["regions"])
        record["plan_fingerprint"] = key
        write_control(control_path, values, snapshot["look"]["spec"]["channels"])
        return {"control": control_path, "report": record}

    # -- audition path ----------------------------------------------------
    def preview(self, reel_id, revision=None, overrides=None):
        """Project source silence into the requested revision for audition.

        The source-time silence cache (content and thresholds keyed) is reused,
        but the reel-time projection is rebuilt from this revision on every
        call, so audition reports where this cut would actually be ducked.
        """
        reel = self.reels.get(reel_id, revision)
        document = reel["document"]
        tracks = document.get("tracks")
        require(tracks is not None and "narration" in tracks and "background" in tracks,
                "mix_tracks", "Ducking preview requires a background and a narration track")
        settings = normalize_ducking(overrides or {}, document.get("ducking"))
        narration = tracks["narration"]
        narration_id = narration["asset"]
        # Scaling the narration by its track gain shifts every level by that
        # gain, so the equivalent source-domain threshold is T - gain.  Asking
        # the content-addressed analysis for that threshold keeps this preview
        # consistent with the render without touching the narration right now.
        threshold = max(-100.0, min(-1.0, settings["threshold_db"] - narration["gain_db"]))
        analysis = self.analysis.run(narration_id, "signal",
                                     {"threshold_db": threshold,
                                      "minimum_silence": settings["minimum_silence"]})
        result = analysis["result"]
        speech = speech_windows(result["silence"], result["duration_tick"])
        sources = {}
        for track in tracks.values():
            source = self.assets.get(track["asset"], ready=True)
            sources[track["asset"]] = {"blob": source["blob"], "metadata": source["metadata"]}
        windows = project_speech(speech, narration, document["duration_tick"])
        regions = envelope_regions(windows, settings, document["duration_tick"])
        identity = {"reel": reel["id"], "revision": reel["revision"], "reel_hash": reel["hash"]}
        record, _ = self._record(identity, document["duration_tick"], tracks, sources,
                                 settings, speech, windows, regions)
        return {**record, "analysis": analysis["id"], "analysis_settings": analysis["settings"]}
