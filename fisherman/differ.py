import io
from dataclasses import dataclass

import imagehash
from PIL import Image

_MAX_DHASH_DISTANCE = 64  # 8x8 hash → 64 bits


@dataclass(frozen=True, slots=True)
class DiffResult:
    is_new: bool
    distance: int  # 0–64, higher = more different


class FrameDiffer:
    def __init__(self, threshold: int = 6):
        self._threshold = threshold
        self._last_hash: imagehash.ImageHash | None = None

    def diff_frame(self, jpeg_data: bytes) -> DiffResult:
        """Compare frame against last accepted frame. ~1ms."""
        img = Image.open(io.BytesIO(jpeg_data))
        h = imagehash.dhash(img, hash_size=8)
        if self._last_hash is None:
            self._last_hash = h
            return DiffResult(is_new=True, distance=_MAX_DHASH_DISTANCE)
        distance = int(h - self._last_hash)
        if distance < self._threshold:
            return DiffResult(is_new=False, distance=distance)
        self._last_hash = h
        return DiffResult(is_new=True, distance=distance)

    def is_new(self, jpeg_data: bytes) -> bool:
        """Backward-compat wrapper around diff_frame()."""
        return self.diff_frame(jpeg_data).is_new
