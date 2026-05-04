from dataclasses import asdict, dataclass

from fisherman.config import FishermanConfig


@dataclass(frozen=True, slots=True)
class RoutingSignals:
    dhash_distance: int
    ocr_text_length: int
    ocr_url_count: int
    bundle_id: str
    is_text_heavy_app: bool


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    tier_hint: int  # 1 = text-only sufficient; 2 = visual content, needs richer processing
    signals: RoutingSignals

    def to_wire(self) -> dict:
        return {
            "tier_hint": self.tier_hint,
            "routing_signals": asdict(self.signals),
        }


class TierRouter:
    def __init__(self, config: FishermanConfig):
        self._text_heavy = set(config.text_heavy_bundles)
        self._dhash_thresh = config.dhash_escalation_threshold
        self._ocr_min = config.ocr_min_text_length

    def route(
        self,
        dhash_distance: int,
        ocr_text: str,
        urls: list[str],
        bundle_id: str,
    ) -> RoutingDecision:
        is_text_heavy = bundle_id in self._text_heavy
        ocr_len = len(ocr_text)
        signals = RoutingSignals(
            dhash_distance=dhash_distance,
            ocr_text_length=ocr_len,
            ocr_url_count=len(urls),
            bundle_id=bundle_id,
            is_text_heavy_app=is_text_heavy,
        )
        tier = self._decide(dhash_distance, ocr_len, is_text_heavy)
        return RoutingDecision(tier_hint=tier, signals=signals)

    def _decide(self, dhash: int, ocr_len: int, text_heavy: bool) -> int:
        low_visual_change = dhash < self._dhash_thresh
        # Tier 1: text-heavy app with enough OCR text and small visual change
        if text_heavy and ocr_len >= self._ocr_min and low_visual_change:
            return 1
        # Tier 1: any app with abundant OCR text and small visual change
        if ocr_len >= 150 and low_visual_change:
            return 1
        # Visual content or large change — server can apply richer processing
        return 2
