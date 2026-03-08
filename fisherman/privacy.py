import threading

from fisherman.config import FishermanConfig


class PrivacyFilter:
    def __init__(self, config: FishermanConfig):
        self._excluded_bundles = set(b.lower() for b in config.excluded_bundles)
        self._excluded_apps = set(a.lower() for a in config.excluded_apps)
        self._paused = False
        self._lock = threading.Lock()

    def should_skip(self, bundle_id: str | None, app_name: str | None) -> bool:
        if self._paused:
            return True
        if bundle_id and bundle_id.lower() in self._excluded_bundles:
            return True
        if app_name and app_name.lower() in self._excluded_apps:
            return True
        return False

    def pause(self) -> None:
        with self._lock:
            self._paused = True

    def resume(self) -> None:
        with self._lock:
            self._paused = False

    @property
    def is_paused(self) -> bool:
        return self._paused
