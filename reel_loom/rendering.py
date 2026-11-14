import logging
import threading
from .errors import LoomError
from .probe import inspect_media
from .timebase import seconds, TICKS_PER_SECOND

logger = logging.getLogger(__name__)


class Renderer:
    def __init__(self, settings, blobs, tickets, planner, processes, artifacts, mixing):
        self.settings, self.blobs, self.tickets = settings, blobs, tickets
        self.planner, self.processes, self.artifacts = planner, processes, artifacts
        self.mixing = mixing
        self.stopping = threading.Event()
        self.threads = []

    def execute(self, ticket):
        ticket_id = ticket["id"]
        snapshot = ticket["snapshot"]
        suffix = ".mp4" if snapshot["look"]["spec"]["kind"] == "video" else ".wav"
        try:
            with self.blobs.temporary(suffix) as output, self.blobs.temporary(".wav") as control:
                mix = None
                if snapshot["document"].get("tracks"):
                    mix = self.mixing.prepare(snapshot, lambda key: self.blobs.resolve(key, verify=True), control)
                args = self.planner.compile(snapshot, lambda key: self.blobs.resolve(key, verify=True), output, mix)
                duration = seconds(snapshot["document"]["duration_tick"])
                self.processes.run(args,
                    cancelled=lambda: self.stopping.is_set() or self.tickets.cancellation_requested(ticket_id),
                    progress=lambda value: self.tickets.progress(ticket_id, value), duration=duration)
                metadata = inspect_media(output, self.settings)
                actual = seconds(metadata["duration_tick"])
                tolerance = max(0.15, 2/float(__import__('fractions').Fraction(snapshot["look"]["spec"]["frame_rate"])))
                if abs(actual-duration) > tolerance:
                    raise LoomError("rendition_duration", "Encoded duration differs from the frozen reel", 422,
                                    {"expected": duration, "actual": actual})
                encoded = {**metadata, "fingerprint": ticket["fingerprint"]}
                extras = []
                if mix is not None:
                    encoded["mix"] = mix["report"]
                    if mix.get("control") is not None:
                        # The exact gain trace used for this mix is frozen next
                        # to the rendition, so a delivery can prove what the bed
                        # received and when.
                        extras.append(("ducking_control", mix["control"], {
                            "algorithm": mix["report"]["algorithm"],
                            "envelope": mix["report"]["envelope"],
                            "plan_fingerprint": mix["report"]["plan_fingerprint"],
                            "source_sample_rate": mix["report"]["tracks"]["background"]["source_sample_rate"],
                            "fingerprint": ticket["fingerprint"]}))
                return self.artifacts.commit_render(ticket_id, output, encoded, extras)
        except LoomError as exc:
            self.tickets.fail(ticket_id, exc)
            return None
        except Exception:
            logger.exception("Render failed unexpectedly: %s", ticket_id)
            self.tickets.fail(ticket_id, LoomError("internal_render", "Unexpected rendering failure", 500))
            return None

    def run_once(self):
        ticket = self.tickets.claim()
        if ticket is None:
            return None
        self.execute(ticket)
        return self.tickets.get(ticket["id"])

    def loop(self):
        while not self.stopping.is_set():
            if self.run_once() is None:
                self.stopping.wait(.2)

    def start(self):
        self.stopping.clear()
        for index in range(self.settings.workers):
            thread = threading.Thread(target=self.loop, name=f"loom-render-{index}", daemon=True)
            thread.start()
            self.threads.append(thread)

    def close(self):
        self.stopping.set()
        for thread in self.threads:
            thread.join(timeout=5)
        self.threads.clear()
