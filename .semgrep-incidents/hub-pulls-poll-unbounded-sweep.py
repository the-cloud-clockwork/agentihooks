SLICE_MS = 10_000


class Sweeping:
    def poll(self) -> None:
        # ruleid: hub-pulls-poll-unbounded-sweep
        for url in self._open():
            try:
                seen = self._fetch(url)
            except KeyError:
                continue
            self.observe(seen)


class Sliced:
    def poll(self) -> None:
        now = self._clock()
        # ok: hub-pulls-poll-unbounded-sweep
        for url in self._open():
            if self._clock() >= now + SLICE_MS:
                self._pause(url)
                return
            try:
                seen = self._fetch(url)
            except KeyError:
                continue
            self.observe(seen)
