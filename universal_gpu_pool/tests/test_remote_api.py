"""Test del backend API gratuite contro un finto server OpenAI (nessuna rete esterna)."""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import remote_api_backend as rab  # noqa: E402
from core.remote_api_backend import Provider, RemoteAPIBackend, NoProviderError  # noqa: E402
from tests.fake_openai import FakeOpenAI  # noqa: E402


class RemoteAPITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = FakeOpenAI(rate_n=1).start()

    @classmethod
    def tearDownClass(cls):
        cls.fake.stop()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env_backup = dict(os.environ)
        self.fake.calls.clear()
        self.fake._rate_seen = 0

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env_backup)
        self.tmp.cleanup()

    def be(self, specs, **kw):
        """specs: lista di (nome, chiave) -> provider che puntano al finto server."""
        provs = []
        for name, key in specs:
            env = f"T_{name.upper()}_KEY"
            if key is None:
                os.environ.pop(env, None)
            else:
                os.environ[env] = key
            provs.append(Provider(name, self.fake.base_url, "fake-model", env, rpm=kw.pop(f"rpm_{name}", 100),
                                  rpd=kw.pop(f"rpd_{name}", 1000)))
        d = Path(self.tmp.name)
        return RemoteAPIBackend(provs, cache_dir=d / "cache", state_file=d / "usage.json",
                                total_timeout=kw.get("total_timeout", 20), request_timeout=5)

    def msg(self, text):
        return [{"role": "user", "content": text}]

    def test_ok_and_cache(self):
        be = self.be([("a", "good")])
        r1 = be.generate(self.msg("ciao"))
        self.assertEqual(r1["text"], "OK: ciao")
        self.assertFalse(r1["cached"])
        r2 = be.generate(self.msg("ciao"))
        self.assertTrue(r2["cached"])
        self.assertEqual(len(self.fake.calls), 1, "la seconda richiesta deve venire dalla cache")

    def test_no_keys_clear_error(self):
        be = self.be([("a", None), ("b", None)])
        with self.assertRaises(NoProviderError) as cm:
            be.generate(self.msg("x"))
        self.assertIn("Account e chiavi", str(cm.exception))
        self.assertEqual(self.fake.calls, [])

    def test_rotation_on_401(self):
        be = self.be([("a", "bad"), ("b", "good")])
        r = be.generate(self.msg("ruota"))
        self.assertEqual(r["provider"], "b")
        st = be.status()
        self.assertTrue(st["a"]["disabled"])
        self.assertIn("401", st["a"]["last_error"])
        # il provider disabilitato non viene più chiamato
        n = len(self.fake.calls)
        be.generate(self.msg("altro"))
        self.assertEqual([c["key"] for c in self.fake.calls[n:]], ["good"])

    def test_all_invalid_fails_fast(self):
        be = self.be([("a", "bad"), ("b", "nomodel")])
        t0 = time.time()
        with self.assertRaises(NoProviderError):
            be.generate(self.msg("x"))
        self.assertLess(time.time() - t0, 3)
        self.assertIn("modello", be.status()["b"]["last_error"])

    def test_429_retry_after_then_ok(self):
        be = self.be([("a", "rate")])
        t0 = time.time()
        r = be.generate(self.msg("attendi"))
        self.assertEqual(r["text"], "OK: attendi")
        self.assertGreaterEqual(time.time() - t0, 0.9, "deve rispettare Retry-After")

    def test_429_daily_pauses_until_midnight_and_fails_fast(self):
        be = self.be([("a", "daily")], total_timeout=30)
        t0 = time.time()
        with self.assertRaises(NoProviderError) as cm:
            be.generate(self.msg("x"))
        self.assertLess(time.time() - t0, 3, "con quota giornaliera esaurita non deve aspettare inutilmente")
        self.assertIn("pausa", str(cm.exception))
        self.assertGreater(be.status()["a"]["cooldown_s"], 59)

    def test_429_per_minute_is_not_daily(self):
        be = self.be([("a", "minute"), ("b", "good")])
        r = be.generate(self.msg("x"))
        self.assertEqual(r["provider"], "b")
        cd = be.status()["a"]["cooldown_s"]
        self.assertLessEqual(cd, 300, "un limite al minuto non deve bloccare fino a mezzanotte")

    def test_empty_response_goes_to_next(self):
        be = self.be([("a", "empty"), ("b", "good")])
        self.assertEqual(be.generate(self.msg("x"))["provider"], "b")

    def test_think_tags_removed(self):
        be = self.be([("a", "think")])
        self.assertEqual(be.generate(self.msg("x"))["text"], "Risposta finale")

    def test_500_rotates(self):
        be = self.be([("a", "err500"), ("b", "good")])
        self.assertEqual(be.generate(self.msg("x"))["provider"], "b")

    def test_rpm_limit_respected(self):
        be = self.be([("a", "good")], rpm_a=2)
        for i in range(2):
            be.generate(self.msg(f"m{i}"))
        self.assertGreater(be._try_acquire(be.providers[0]), 0, "terza richiesta nello stesso minuto bloccata")

    def test_rpd_persisted(self):
        be = self.be([("a", "good")])
        be.generate(self.msg("uno"))
        be.generate(self.msg("due"))
        be2 = RemoteAPIBackend(be.providers, cache_dir=be.cache_dir, state_file=be.state_file)
        self.assertEqual(be2.status()["a"]["used_today"], 2)

    def test_run_batch_order_and_partial_errors(self):
        be = self.be([("a", "good")])
        items = [{"id": str(i), "prompt": f"p{i}"} for i in range(10)]
        seen = []
        res = be.run_batch(items, workers=4, on_result=seen.append)
        self.assertEqual([r["id"] for r in res], [str(i) for i in range(10)])
        self.assertTrue(all(r["ok"] for r in res))
        self.assertEqual(len(seen), 10)

    def test_test_provider(self):
        be = self.be([("a", "good"), ("b", "bad"), ("c", None)])
        self.assertTrue(be.test_provider("a")["ok"])
        r = be.test_provider("b")
        self.assertFalse(r["ok"]); self.assertIn("401", r["error"])
        self.assertIn("chiave mancante", be.test_provider("c")["error"])
        self.assertFalse(be.test_provider("zzz")["ok"])

    def test_providers_file_roundtrip_and_new_defaults(self):
        f = Path(self.tmp.name) / "providers.json"
        provs = rab.default_providers()[:2]
        provs[0].model = "altro-modello"
        rab.save_providers(provs, f)
        loaded = rab.load_providers(f)
        self.assertEqual(loaded[0].model, "altro-modello")
        # i provider di default mancanti dal file vengono aggiunti in coda
        self.assertEqual({p.name for p in loaded}, {p.name for p in rab.DEFAULT_PROVIDERS})
        f.write_text("{rotto")
        self.assertEqual(len(rab.load_providers(f)), len(rab.DEFAULT_PROVIDERS))

    def test_disabled_and_urlless_providers_skipped(self):
        be = self.be([("a", "good"), ("b", "good")])
        be.providers[0].enabled = False
        be.providers[1].base_url = ""
        with self.assertRaises(NoProviderError):
            be.generate(self.msg("x"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
