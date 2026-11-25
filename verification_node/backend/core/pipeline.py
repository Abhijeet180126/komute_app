"""The verification engine: decode, stages, decision. Stores nothing: the caller keeps the outcome.
Fails closed: a fault is an error, never a decision."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import numpy as np

from . import hardware
from .aamva import AamvaRecord
from .config import Settings
from .contracts import (
    Advisory,
    Claimed,
    EngineError,
    InputError,
    Reason,
    StageResult,
)
from .imaging import IMAGE_KEYS, decode_image
from .monitor import EngineMonitor

log = logging.getLogger("komute.engine")

@dataclass(frozen=True)
class VerifyInput:
    request_id: str
    driver_id: str
    consent_at: datetime
    consent_policy_version: str
    claimed: Claimed
    image_bytes: dict[str, bytes]


@dataclass
class Stages:
    """The three stages as callables, so tests can inject fakes and a stage crash can be simulated."""

    barcode: Callable[[np.ndarray, Claimed, datetime], tuple[StageResult, Optional[AamvaRecord]]]
    face: Callable[[np.ndarray, np.ndarray], StageResult]
    vehicle: Callable[[np.ndarray, Claimed], StageResult]
    liveness_skipped: bool = False


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _skipped(name: str) -> StageResult:
    return StageResult(name=name, ran=False)  # type: ignore[arg-type]


class Engine:
    def __init__(self, settings: Settings, stages: Stages, clock: Callable[[], datetime] = _utc_now) -> None:
        self._s = settings
        self._stages = stages
        self._clock = clock
        self.health_info: Callable[[], dict] = lambda: {"models_loaded": {}}
        self.models_info: Callable[[], list[dict]] = lambda: []
        self.monitor = EngineMonitor()

    def verify(self, inp: VerifyInput) -> dict:
        self.monitor.begin()
        try:
            return self._run(inp)
        except InputError:
            self.monitor.abandon()
            raise
        except Exception as e:  # the one fail-closed boundary: any fault is an error, never a decision
            self.monitor.failed()
            log.error("engine fault request_id=%s type=%s", inp.request_id, type(e).__name__)
            raise EngineError("ENGINE_ERROR") from e
        finally:
            hardware.trim_heap()

    def _run(self, inp: VerifyInput) -> dict:
        with hardware.MemorySampler() as mem:
            return self._run_sampled(inp, mem)

    def _run_sampled(self, inp: VerifyInput, mem: hardware.MemorySampler) -> dict:
        t_start = time.perf_counter()
        now = self._clock()
        images = {k: decode_image(inp.image_bytes[k], self._s.max_image_side) for k in IMAGE_KEYS}

        mem.mark("decoded")
        self.monitor.stage("barcode")
        barcode, record = self._stages.barcode(images["licence_back"], inp.claimed, now)
        mem.mark("barcode")
        if barcode.reasons:
            face, vehicle = _skipped("face"), _skipped("vehicle")
        else:
            self.monitor.stage("face")
            face = self._stages.face(images["selfie"], images["licence_front"])
            mem.mark("face")
            self.monitor.stage("vehicle")
            vehicle = self._stages.vehicle(images["vehicle"], inp.claimed)
            mem.mark("vehicle")
        results = [barcode, face, vehicle]
        self.monitor.stage("decision")

        reasons: list[Reason] = []
        for r in results:
            for reason in r.reasons:
                if all(x.code != reason.code for x in reasons):
                    reasons.append(reason)
        decision = "APPROVED" if all(r.ran for r in results) and not reasons else "REJECTED"
        advisories = [a.value for r in results for a in r.advisories]
        if self._stages.liveness_skipped:
            advisories.append(Advisory.LIVENESS_SKIPPED_DEV.value)

        timings = {"total": 0.0}
        for r in results:
            timings[r.name] = round(sum(r.timings_ms.values()), 1)
        # `passed` is present only on a stage that ran: it ran and raised no reason.
        stage_view: dict[str, Any] = {
            r.name: ({"ran": True, "passed": not r.reasons, **r.details} if r.ran else {"ran": False}) for r in results
        }

        timings["total"] = round((time.perf_counter() - t_start) * 1000, 1)
        mem.mark("saved")
        response = {
            "request_id": inp.request_id,
            "decision": decision,
            "reasons": [{"code": r.code.value, "retryable": r.retryable, "message": r.message} for r in reasons],
            "stages": stage_view,
            "advisories": advisories,
            "timings_ms": timings,
            "rss_mb": round(hardware.rss_mb(), 1),
            "memory_trace": mem.trace(),
        }
        if self._s.response_barcode_fields and record is not None:
            response["barcode_fields"] = dict(record.fields)
        self.monitor.done(response, {f"{r.name}.{k}": v for r in results for k, v in r.timings_ms.items()})
        return response


def _warm_up(detector, embedder_session, plate_detector, plate_ocr) -> None:
    """One blank inference per model, so the first real request does not pay the ONNX start-up cost."""
    import numpy as np

    from .stages.face import embed
    from .stages.vehicle import detect_plate, read_plate

    t = time.perf_counter()
    try:
        detector.detect(np.zeros((480, 640, 3), np.uint8))
        embed(np.zeros((112, 112, 3), np.uint8), embedder_session)
        detect_plate(np.zeros((480, 640, 3), np.uint8), plate_detector)
        read_plate(np.zeros((64, 128, 3), np.uint8), plate_ocr)
        log.info("models warmed in %.0f ms, rss %.0f MB", (time.perf_counter() - t) * 1000, hardware.rss_mb())
    except Exception as e:  # warm-up is an optimisation; a failure must not stop the node
        log.warning("model warm-up skipped type=%s", type(e).__name__)


def build_engine(settings: Settings, clock: Callable[[], datetime] = _utc_now) -> Engine:
    from .liveness import DevSkipLiveness, OnnxLiveness
    from .registry import ModelRegistry
    from .stages.barcode import run_barcode_stage
    from .stages.face import FaceDetector, embed, run_face_stage
    from .stages.vehicle import detect_plate, read_plate, run_vehicle_stage

    registry = ModelRegistry(settings.models_dir, settings.models_dir / "models.lock", settings.liveness_model)
    registry.load()
    detector = FaceDetector(registry.face_detector_path, settings.face_detect_score)
    embedder_session = registry.session("face_embedder")
    plate_detector, plate_ocr = registry.session("plate_detector"), registry.session("plate_ocr")

    if settings.liveness_model is not None:
        liveness: Any = OnnxLiveness(
            registry.session("liveness"),
            settings.liveness_crop_scale,
            settings.liveness_input_size,
            settings.liveness_live_index,
        )
        skipped = False
    elif settings.dev_skip_liveness:
        liveness, skipped = DevSkipLiveness(), True
    else:
        raise EngineError("LIVENESS_MODEL_MISSING")

    stages = Stages(
        barcode=lambda back, claimed, now: run_barcode_stage(
            back, claimed, now, settings.barcode_budget_s, settings.name_match_threshold, settings.min_age
        ),
        face=lambda selfie, front: run_face_stage(
            selfie, front, detector, lambda crop: embed(crop, embedder_session), liveness, settings
        ),
        vehicle=lambda img, claimed: run_vehicle_stage(
            img,
            claimed,
            lambda i: detect_plate(i, plate_detector, settings.plate_detect_score),
            lambda c: read_plate(c, plate_ocr),
            settings,
        ),
        liveness_skipped=skipped,
    )
    if settings.warmup_max_rss_mb > 0 and hardware.rss_mb() < settings.warmup_max_rss_mb:
        _warm_up(detector, embedder_session, plate_detector, plate_ocr)
    engine = Engine(settings, stages, clock)
    engine.health_info = lambda: {"models_loaded": dict(registry.loaded)}
    engine.models_info = registry.describe
    return engine
