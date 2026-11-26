import threading, time


class Watchdog(threading.Thread):
    """Controlla i lease dei worker e rimette in coda gli step dei nodi silenziosi."""
    def __init__(self, router, interval=5.0):
        super().__init__(daemon=True)
        self.router, self.interval, self._halt = router, interval, threading.Event()

    def run(self):
        while not self._halt.wait(self.interval):
            self.router.reap()

    def stop(self):
        self._halt.set()
