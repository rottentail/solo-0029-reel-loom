"""A bounded production walkthrough using generated audiovisual media and HTTP."""
import argparse
from dataclasses import replace
import json
import math
from array import array
from pathlib import Path
import tempfile
import threading
import time
import wave
from .config import Settings
from .application import Application
from .api import Api
from .http import MediaHttpServer
from .client import Client
from .errors import LoomError, require

WORKFLOWS = ("ingest", "revisions", "looks", "render", "signal", "waveform", "loudness",
             "sheet", "captions", "imprint", "cancel-retry", "recovery", "resources", "matrix", "conformance", "lineage", "ducking")

# Ducking walkthrough geometry, in seconds of reel time.  Every phrase is
# deliberately scheduled to straddle a hard cut.
MIX_SLICE = 1.0           # six one-second cuts
MIX_CUTS = 6
MIX_DURATION = MIX_SLICE * MIX_CUTS
MIX_BED_TONE = 220        # low, separated from narration
MIX_VOICE_TONE = 1500     # high, separated from the bed
MIX_BED_RATE = 44100      # stereo background
MIX_VOICE_RATE = 22050    # mono narration
MIX_ALT_RATE = 32000      # alternate recording
MIX_NARRATION_AT = 0.35   # narration begins just before the first cut
MIX_SPEECH = ((0.4, 1.1), (2.0, 2.7), (3.6, 4.4))   # narration source windows
MIX_ALT_SPEECH = ((1.5, 2.1), (3.0, 3.6))
MIX_CUT_TIMES = tuple(round(MIX_SLICE * index, 3) for index in range(1, MIX_CUTS))
MIX_WINDOWS = tuple((round(MIX_NARRATION_AT + a, 6), round(MIX_NARRATION_AT + b, 6))
                    for a, b in MIX_SPEECH)
MIX_ALT_WINDOWS = tuple((round(MIX_NARRATION_AT + a, 6), round(MIX_NARRATION_AT + b, 6))
                        for a, b in MIX_ALT_SPEECH)


def tone_wav(path, rate, channels, duration, frequency, amplitude, windows=None):
    """A pure sine with optional silent gaps, written directly as PCM."""
    frames = int(round(rate * duration))
    samples = array("h")
    for index in range(frames):
        second = index / rate
        level = amplitude if (windows is None or any(a <= second < b for a, b in windows)) else 0.0
        sample = int(round(math.sin(2 * math.pi * frequency * second) * level * 32767))
        samples.extend((sample, sample) if channels == 2 else (sample,))
    with wave.open(str(path), "wb") as media:
        media.setnchannels(channels)
        media.setsampwidth(2)
        media.setframerate(rate)
        media.writeframes(samples.tobytes())
    return path


def decode_mono(processes, path, rate=16000):
    """Decode a rendition to mono PCM once so many probes share one pass."""
    with tempfile.TemporaryDirectory(prefix="loom-probe-") as folder:
        pcm = Path(folder) / "probe.pcm"
        processes.decode_audio(path, pcm, rate)
        values = array("h")
        values.frombytes(pcm.read_bytes())
    return rate, values


def tone_level(rate, samples, start, duration, frequency):
    """Goertzel level (dBFS) of one tone at a reel position."""
    begin = int(round(start * rate))
    length = max(1, int(round(duration * rate)))
    omega = 2 * math.pi * frequency / rate
    real = imaginary = 0.0
    for offset in range(length):
        sample = samples[begin + offset] / 32768
        real += sample * math.cos(omega * offset)
        imaginary -= sample * math.sin(omega * offset)
    amplitude = 2 * math.hypot(real, imaginary) / length
    return round(20 * math.log10(max(amplitude, 1e-9)), 2)


def expect_error(action, code):
    try:
        action()
    except LoomError as exc:
        require(exc.code == code, "walkthrough_error", "Unexpected workflow rejection", expected=code, actual=exc.code)
        return exc.as_dict()
    raise RuntimeError("The walkthrough expected an operation to be rejected: " + code)


class Walkthrough:
    def __init__(self, settings):
        self.settings = settings
        self.app = Application(settings, start_workers=False)
        self.server = MediaHttpServer(("127.0.0.1", 0), Api(self.app))
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
        self.thread.start()
        self.client = Client(f"http://127.0.0.1:{self.server.server_port}/loom/v1")

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.app.close()

    def setup(self):
        with self.app.blobs.temporary(".mp4") as sample:
            self.app.processes.run(["-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=3",
                                    "-f", "lavfi", "-i", "sine=frequency=659:sample_rate=48000:duration=3",
                                    "-af", "volume=enable='between(t,1,1.5)':volume=0",
                                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(sample)])
            self.content = sample.read_bytes()
        self.asset = self.client.ingest(self.content, "harbor-tone")
        self.slices = [{"asset": self.asset["id"], "in": .2, "out": 1.2, "label": "开场"},
                       {"asset": self.asset["id"], "in": 1.6, "out": 2.8, "label": "收束"}]
        self.reel = self.client.create_reel("回声样片", self.slices, "demo-reel")
        self.look = self.client.put_look("preview", {"kind": "video", "width": 320, "height": 180})
        self.audio_look = self.client.put_look("listening", {"kind": "audio", "sample_rate": 48000})
        self.ticket = self.client.render(self.reel["id"], "preview", key="demo-render")
        return self

    def finish(self):
        ticket = self.client.request("GET", "/tickets/"+self.ticket["id"])
        if ticket["state"] == "queued":
            self.app.renderer.run_once()
        ticket = self.client.wait(self.ticket["id"], timeout=5)
        require(ticket["state"] == "succeeded", "walkthrough_render", "Walkthrough render did not succeed", ticket=ticket)
        return ticket

    def run(self, name):
        c, a = self.client, self.asset["id"]
        if name == "ingest":
            duplicate = c.ingest(self.content, "duplicate-name")
            require(duplicate["id"] == a, "walkthrough_dedup", "Identical ready media should be reused")
            failure = expect_error(lambda: c.ingest(b"not-media", "bad"), "unreadable_media")
            return {"asset": self.asset, "deduplicated": True, "failure": failure}
        if name == "revisions":
            updated = c.replace_reel(self.reel["id"], 1, "回声样片·修订", list(reversed(self.slices)))
            old = c.request("GET", "/reels/"+self.reel["id"]+"?revision=1")
            require(old["hash"] == self.reel["hash"] and updated["revision"] == 2,
                    "walkthrough_revision", "Historical reel changed")
            failure = expect_error(lambda: c.replace_reel(self.reel["id"], 1, "旧稿", self.slices), "revision_conflict")
            return {"head": updated["head"], "historical_hash": old["hash"], "failure": failure}
        if name == "looks":
            new = c.put_look("preview", {"kind": "video", "width": 640, "height": 360}, 1)
            ticket = c.request("GET", "/tickets/"+self.ticket["id"])
            require(ticket["look_version"] == 1 and new["version"] == 2, "walkthrough_look", "Queued look drifted")
            failure = expect_error(lambda: c.put_look("preview", {"kind":"video"}, 1), "revision_conflict")
            return {"new_version": 2, "frozen_version": ticket["look_version"], "failure": failure}
        if name == "render":
            ticket = self.finish()
            body = {"reel": self.reel["id"], "look": "preview"}
            replay = c.request("POST", "/tickets", body, {"Idempotency-Key":"demo-render"})
            require(replay["id"] == ticket["id"], "walkthrough_idempotency", "Render replay changed identity")
            failure = expect_error(lambda: c.request("POST", "/tickets", {**body,"look":"listening"},
                                                   {"Idempotency-Key":"demo-render"}), "idempotency_conflict")
            audio = c.render(self.reel["id"], "listening")
            self.app.renderer.run_once()
            audio = c.wait(audio["id"], 5)
            require(audio["state"] == "succeeded", "walkthrough_audio", "Audio rendition did not succeed")
            return {"video": ticket, "audio": audio, "failure": failure}
        if name in ("signal", "waveform", "loudness"):
            result = c.request("POST", f"/assets/{a}/{name}", {})
            replay = c.request("POST", f"/assets/{a}/{name}", {})
            require(result["id"] == replay["id"], "walkthrough_analysis", "Analysis cache should be content/settings stable")
            failure = expect_error(lambda: c.request("POST", f"/assets/{a}/{name}", {"unsupported": 1}), "unknown_fields")
            return {"analysis": result, "cached": True, "failure": failure}
        if name == "sheet":
            result = c.request("POST", f"/assets/{a}/contact-sheet", {"count": 6, "columns": 3})
            media = c.request("GET", "/artifacts/"+result["id"]+"/content", raw=True)
            require(media.startswith(b"\xff\xd8"), "walkthrough_sheet", "Contact sheet is not JPEG")
            failure = expect_error(lambda: c.request("POST", f"/assets/{a}/contact-sheet", {"count":1,"columns":3}), "sheet_layout")
            return {"artifact": result, "bytes": len(media), "failure": failure}
        if name == "captions":
            track = c.request("POST", f"/assets/{a}/cues", {"language":"zh-CN",
                              "srt":"1\n00:00:00,000 --> 00:00:02,900\n潮声\n"})
            projected = c.request("POST", f'/reels/{self.reel["id"]}/captions', {"revision":1,"tracks":[track["id"]]})
            require(len(projected["cues"]) == 2, "walkthrough_captions", "A cue spanning cuts must project to both slices")
            failure = expect_error(lambda: c.request("POST", f"/assets/{a}/cues", {"language":"zh","srt":"broken"}), "cue_format")
            return {"projection": projected, "track": track, "failure": failure}
        if name == "imprint":
            failure = expect_error(lambda: c.imprint(self.ticket["id"]), "imprint_state")
            ticket = self.finish()
            imprint = c.imprint(ticket["id"])
            verified = c.request("GET", f'/imprints/{imprint["id"]}/verify')
            self.client.replace_reel(self.reel["id"], 1, "新的草剪", list(reversed(self.slices)))
            unchanged = c.imprint(ticket["id"])
            require(imprint["id"] == unchanged["id"], "walkthrough_imprint", "Upstream editing changed frozen imprint")
            return {"imprint": imprint, "verified": verified, "failure": failure}
        if name == "cancel-retry":
            cancelled = c.request("POST", f'/tickets/{self.ticket["id"]}/cancel')
            require(cancelled["state"] == "cancelled", "walkthrough_cancel", "Queued render did not cancel")
            retried = c.request("POST", f'/tickets/{self.ticket["id"]}/retry')
            ticket = self.finish()
            failure = expect_error(lambda: c.request("POST", f'/tickets/{self.ticket["id"]}/retry'), "retry_state")
            require(retried["attempt"] == 2, "walkthrough_attempt", "Retry attempt was not incremented")
            return {"ticket": ticket, "failure": failure}
        if name == "recovery":
            claimed = self.app.tickets.claim()
            self.close()
            with Application(self.settings, start_workers=False) as restarted:
                recovered = restarted.tickets.get(claimed["id"])
                require(recovered["state"] == "failed" and recovered["error"]["error"] == "worker_interrupted",
                        "walkthrough_recovery", "Interrupted render was not recovered")
                restarted.tickets.retry(claimed["id"])
                finished = restarted.renderer.run_once()
                require(finished["state"] == "succeeded", "walkthrough_recovery", "Recovered render could not retry")
                return {"interrupted": recovered, "retried_state": finished["state"]}
        if name == "resources":
            c.request("POST", f"/assets/{a}/retire")
            failure = expect_error(lambda: c.create_reel("退役来源", self.slices), "asset_retired")
            ticket = self.finish()
            sweep = c.request("POST", "/resources/sweep", {"dry_run":False})
            status = c.request("GET", "/resources")
            require(status["consistent"] and not sweep["removed"], "walkthrough_resources", "Referenced source was collected")
            return {"reconcile":status,"sweep":sweep,"render_state":ticket["state"],"failure":failure}
        if name == "matrix":
            body = {"reel":self.reel["id"],"looks":[{"id":"preview"},{"id":"listening"}]}
            matrix = c.request("POST", "/matrices", body, {"Idempotency-Key":"demo-matrix"})
            before = len(c.request("GET", "/tickets")["items"])
            failure = expect_error(lambda: c.request("POST", "/matrices", {**body,"looks":[{"id":"preview"},{"id":"missing-look"}]}), "not_found")
            require(len(c.request("GET", "/tickets")["items"]) == before, "walkthrough_matrix", "Invalid matrix committed partial work")
            while self.app.renderer.run_once() is not None:
                pass
            finished = c.request("GET", "/matrices/"+matrix["id"])
            require(finished["state"] == "succeeded", "walkthrough_matrix", "Matrix did not complete")
            return {"matrix": finished,"failure":failure}
        if name == "conformance":
            ticket = self.finish()
            artifact = ticket["artifacts"][0]
            result = c.request("GET", f'/artifacts/{artifact["id"]}/conformance')
            require(result["valid"], "walkthrough_conformance", "Rendered media violates frozen look", report=result)
            failure = expect_error(lambda: c.request("GET", "/artifacts/missing/conformance"), "not_found")
            return {"conformance":result,"failure":failure}
        if name == "lineage":
            self.finish()
            imprint = c.imprint(self.ticket["id"])
            result = c.request("GET", f'/tickets/{self.ticket["id"]}/lineage')
            require(any(n["id"] == imprint["id"] for n in result["nodes"]), "walkthrough_lineage", "Imprint missing from lineage")
            failure = expect_error(lambda: c.request("GET", "/tickets/missing/lineage"), "not_found")
            return {"lineage":result,"failure":failure}
        if name == "ducking":
            return self.ducking()
        raise ValueError("Unknown walkthrough: " + name)

    def ducking(self):
        """Prove ducking across a cut, at mixed sample rates, through the imprint."""
        self.finish()   # settle the queued walkthrough render before mixing
        c = self.client
        with self.app.blobs.temporary(".wav") as bed_path, \
             self.app.blobs.temporary(".wav") as voice_path, \
             self.app.blobs.temporary(".wav") as alt_path:
            tone_wav(bed_path, MIX_BED_RATE, 2, MIX_DURATION + .4, MIX_BED_TONE, .10)
            tone_wav(voice_path, MIX_VOICE_RATE, 1, MIX_DURATION - .1, MIX_VOICE_TONE, .25, MIX_SPEECH)
            tone_wav(alt_path, MIX_ALT_RATE, 1, MIX_DURATION - .1, MIX_VOICE_TONE, .25, MIX_ALT_SPEECH)
            bed = c.ingest(bed_path.read_bytes(), "bed-44100-stereo")
            voice = c.ingest(voice_path.read_bytes(), "voice-22050-mono")
            alternate = c.ingest(alt_path.read_bytes(), "voice-32000-mono")
        picture = c.ingest(self.content, "silent-picture")
        sources = {name: item["metadata"]["streams"][0]
                   for name, item in (("bed", bed), ("voice", voice), ("alternate", alternate))}
        require([v["sample_rate"] for v in sources.values()] == [MIX_BED_RATE, MIX_VOICE_RATE, MIX_ALT_RATE],
                "walkthrough_ducking", "Generated tracks must carry distinct sample rates", actual=sources)
        # Six one-second cuts: every narration phrase straddles at least one
        # boundary and the bed runs across all of them as one continuous track.
        cuts = [{"asset": picture["id"], "in": 0, "out": MIX_SLICE}
                for _ in range(MIX_CUTS)]
        tracks = {"background": {"asset": bed["id"], "in": 0, "out": MIX_DURATION + .4, "at": 0},
                  "narration": {"asset": voice["id"], "in": 0, "out": MIX_DURATION - .1,
                                "at": MIX_NARRATION_AT}}
        reel = c.request("POST", "/reels", {"title": "解说样片", "slices": cuts, "tracks": tracks,
                                            "ducking": {"depth_db": -14}})
        c.put_look("mixdown", {"kind": "audio", "sample_rate": 48000, "channels": 2})
        c.put_look("mixed", {"kind": "video", "width": 320, "height": 180})
        preview = c.request("POST", f'/reels/{reel["id"]}/ducking', {})
        require(preview["windows"] == [{"start_tick": round(a * 1e6), "end_tick": round(b * 1e6)}
                                       for a, b in MIX_WINDOWS],
                "walkthrough_ducking", "Preview must project source speech through the reel placement",
                preview=preview["windows"])
        ticket = c.render(reel["id"], "mixdown")
        self.app.renderer.run_once()
        ticket = c.wait(ticket["id"], 30)
        require(ticket["state"] == "succeeded", "walkthrough_ducking", "Ducked mix did not render", ticket=ticket)
        rendition = next(item for item in ticket["artifacts"] if item["kind"] == "rendition")
        record = rendition["metadata"]["mix"]
        # The same mix must work as a film: picture from the cuts, audio from
        # the ducked bed and narration, still conforming to the frozen look.
        film = c.render(reel["id"], "mixed")
        self.app.renderer.run_once()
        film = c.wait(film["id"], 30)
        film_artifact = next(item for item in film["artifacts"] if item["kind"] == "rendition")
        require(film["state"] == "succeeded" and film_artifact["metadata"]["has_video"] and
                film["artifacts"][0]["metadata"]["mix"]["windows"] == record["windows"],
                "walkthrough_ducking", "Video mix diverged from the audio mix", ticket=film.get("error"))
        require(c.request("GET", f'/artifacts/{film_artifact["id"]}/conformance')["valid"],
                "walkthrough_ducking", "Video mix does not conform to the frozen look")
        with self.app.blobs.temporary(".wav") as rendered:
            rendered.write_bytes(c.request("GET", f'/artifacts/{rendition["id"]}/content', raw=True))
            rate, samples = decode_mono(self.app.processes, rendered)
            # Speech lands at reel 0.75-1.45, 2.35-3.05 and 3.95-4.75, each
            # straddling a one-second cut; the gaps between them are open bed.
            probes = {"open": 0.15, "speech": 2.5, "pause": 3.5,
                      "speech_after_cut": 4.2, "pause_late": 1.8}
            levels = {label: tone_level(rate, samples, at, .25, MIX_BED_TONE)
                      for label, at in probes.items()}
            voice_levels = {label: tone_level(rate, samples, at, .25, MIX_VOICE_TONE)
                            for label, at in (("speech", 2.4), ("speech_after_cut", 4.1))}
            # Compare the bed either side of cuts that fall well inside one
            # continuous low-gain region: a per-slice re-application of the
            # gain would snap back to full level for the slice before the cut.
            inside = [cut for cut in MIX_CUT_TIMES
                      if any(region["start_tick"] + 250000 < cut * 1e6
                             < region["end_tick"] - 150000 for region in record["regions"])]
            require(len(inside) >= 2, "walkthrough_ducking",
                    "Walkthrough must exercise cuts well inside a ducked phrase", cuts=inside)
            across_cut = [{"cut": cut,
                           "before": tone_level(rate, samples, cut - .06, .03, MIX_BED_TONE),
                           "after": tone_level(rate, samples, cut + .01, .03, MIX_BED_TONE)}
                          for cut in inside]
        ducked = levels["speech"]
        require(abs((levels["open"] - ducked) - 14) <= 1.5, "walkthrough_ducking",
                "Measured bed reduction differs from the recorded depth",
                open=levels["open"], ducked=ducked, record=record["ducking"])
        require(min(levels["pause"], levels["pause_late"]) - ducked > 8, "walkthrough_ducking",
                "Background did not return between phrases", levels=levels)
        require(abs(voice_levels["speech"] - voice_levels["speech_after_cut"]) <= 1.0,
                "walkthrough_ducking", "Narration was attenuated by its own sidechain", levels=voice_levels)
        steps = [abs(item["before"] - item["after"]) for item in across_cut]
        require(max(steps) <= 2.0, "walkthrough_ducking",
                "Background level jumps at a cut", levels=across_cut)
        require(record["tracks"]["background"]["source_sample_rate"] == MIX_BED_RATE and
                record["tracks"]["narration"]["source_sample_rate"] == MIX_VOICE_RATE,
                "walkthrough_ducking", "Mix record lost the source sample rates", tracks=record["tracks"])
        # Editing the reel must not let the old schedule be reused.
        replaced = c.request("PUT", f'/reels/{reel["id"]}',
                             {"title": "换录音", "slices": cuts,
                              "tracks": {"background": tracks["background"],
                                         "narration": {"asset": alternate["id"], "in": 0,
                                             "out": MIX_DURATION - .1, "at": MIX_NARRATION_AT}}},
                             {"If-Match": "1"})
        swapped = c.request("POST", f'/reels/{reel["id"]}/ducking', {"revision": replaced["revision"]})
        expected_alt = [{"start_tick": round(a * 1e6), "end_tick": round(b * 1e6)}
                        for a, b in MIX_ALT_WINDOWS]
        require(swapped["windows"] == expected_alt, "walkthrough_ducking",
                "Replacing the recording did not change the projected schedule", windows=swapped["windows"])
        require(swapped["windows"] != preview["windows"], "walkthrough_ducking",
                "New recording reused the previous ducking schedule")
        # A queued mix keeps the revision it was frozen against.
        queued = c.render(reel["id"], "mixdown", revision=1)
        c.request("PUT", f'/reels/{reel["id"]}',
                  {"title": "再改", "slices": list(reversed(cuts)),
                   "tracks": tracks}, {"If-Match": "2"})
        self.app.renderer.run_once()
        frozen = c.wait(queued["id"], 30)
        require(frozen["state"] == "succeeded" and frozen["revision"] == 1 and
                frozen["artifacts"][0]["metadata"]["mix"]["windows"] == preview["windows"],
                "walkthrough_ducking", "A queued mix did not keep its frozen revision",
                ticket={k: frozen.get(k) for k in ("state", "revision")})
        # The imprint carries the gain trace and verifies against the record.
        imprint = c.imprint(ticket["id"])
        verified = c.request("GET", f'/imprints/{imprint["id"]}/verify')
        require(verified["valid"] and verified["controls"] and
                verified["controls"][0]["sha256"] == record["envelope"]["sha256"],
                "walkthrough_ducking", "Imprint control differs from the mix record", verified=verified)
        # The legacy single-track reel still renders without a mix record.
        legacy = c.request("POST", "/reels", {"title": "旧单轨",
                                              "slices": [{"asset": bed["id"], "in": 0, "out": MIX_SLICE},
                                                         {"asset": bed["id"], "in": MIX_SLICE, "out": 2.0}]})
        plain = c.render(legacy["id"], "mixdown")
        self.app.renderer.run_once()
        plain = c.wait(plain["id"], 30)
        require(plain["state"] == "succeeded" and "mix" not in plain["artifacts"][0]["metadata"],
                "walkthrough_ducking", "Legacy single-track reel stopped rendering")
        failures = {
            "unknown_track": expect_error(lambda: c.request(
                "POST", "/reels", {"title": "x", "slices": cuts,
                                   "tracks": {"music": tracks["background"]}}), "unknown_fields"),
            "narration_without_bed": expect_error(lambda: c.request(
                "POST", "/reels", {"title": "x", "slices": cuts,
                                   "tracks": {"narration": tracks["narration"]}}), "mix_tracks"),
            "track_past_source": expect_error(lambda: c.request(
                "POST", "/reels", {"title": "x", "slices": cuts,
                                   "tracks": {"background": {"asset": bed["id"], "in": 0, "out": 7.0, "at": 0}}}),
                "track_bounds"),
            "ducking_without_narration": expect_error(lambda: c.request(
                "POST", "/reels", {"title": "x", "slices": cuts,
                                   "tracks": {"background": tracks["background"]},
                                   "ducking": {"depth_db": -10}}), "mix_tracks"),
            "preview_without_tracks": expect_error(lambda: c.request(
                "POST", f'/reels/{legacy["id"]}/ducking', {}), "mix_tracks"),
        }
        return {"preview_windows": preview["windows"], "levels": levels, "voice_levels": voice_levels,
                "across_cut": across_cut, "record": {k: record[k] for k in
                ("algorithm", "ducking", "windows", "regions", "envelope", "tracks")},
                "replaced_windows": swapped["windows"], "queued_revision": frozen["revision"],
                "video_state": film["state"], "imprint": {"id": imprint["id"], "controls": verified["controls"]},
                "sample_rates": {name: value["sample_rate"] for name, value in sources.items()},
                "legacy_state": plain["state"], "failures": failures}


def main():
    parser = argparse.ArgumentParser(description="Run a bounded audiovisual production walkthrough")
    parser.add_argument("--workflow", choices=WORKFLOWS, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="reel-loom-demo-") as folder:
        settings = replace(Settings.from_env(), root=Path(folder), render_timeout=30)
        demo = Walkthrough(settings)
        try:
            demo.setup()
            result = demo.run(args.workflow)
            print(json.dumps({"workflow":args.workflow,"ok":True,"result":result}, ensure_ascii=False))
        finally:
            if args.workflow != "recovery":
                demo.close()


if __name__ == "__main__":
    main()
