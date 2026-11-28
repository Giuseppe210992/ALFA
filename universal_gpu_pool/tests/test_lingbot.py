"""Test dell'integrazione LingBot-Map: runner, adapter, routing GPU, risultati e download.

Il modello vero richiede una GPU NVIDIA, quindi qui si usa un *finto repository* (tests/stub_lingbot)
con lo stesso `demo.py`-shape: argparse + viewer finale. Si verifica tutto il resto della catena:
sostituzione del viewer, salvataggio file, download input (anche ZIP), errori, job in coda
finché non c'è un nodo GPU, elenco e download dei file risultato.

Per provarlo contro il repository VERO (solo se hai una GPU e il repo clonato):
    LINGBOT_MAP_DIR=/percorso/lingbot-map python -m unittest tests.test_lingbot.RealRepoTest
"""
import http.server
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.base_adapter import StepContext  # noqa: E402
from adapters.lingbot_map_adapter import LingBotMapAdapter, _safe_unzip  # noqa: E402
from core.aggregator_api import Router, make_server  # noqa: E402
from core.state_store import open_store  # noqa: E402
from core.watchdog import Watchdog  # noqa: E402
from worker.worker_daemon import Worker  # noqa: E402
from tests.test_system import http as call_http  # noqa: E402

STUB = str(ROOT / "tests" / "stub_lingbot")
TOKEN = "lingbot-test-token"
try:
    import trimesh  # noqa: F401
    HAVE_TRIMESH = True
except ImportError:
    HAVE_TRIMESH = False


def make_images(folder, n=4):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (folder / f"{i:04d}.png").write_bytes(b"\x89PNG fake " + bytes([i]))
    return folder


def make_zip(path, n=4, evil=False, sub="scena"):
    with zipfile.ZipFile(path, "w") as z:
        for i in range(n):
            z.writestr(f"{sub}/{i:04d}.jpg", b"fake-jpg-" + bytes([i]))
        if evil:
            z.writestr("../evil.txt", "pwned")


class FileServer:
    """Serve una cartella su 127.0.0.1 (per testare il download di video/ZIP da URL)."""
    def __init__(self, directory):
        handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=str(directory), **k)  # noqa: E731
        handler.log_message = lambda *a: None
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.srv.RequestHandlerClass.log_message = lambda *a, **k: None
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self):
        self.srv.shutdown(); self.srv.server_close()


class AdapterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="lb_test_"))
        self.model = self.tmp / "fake.pt"
        self.model.write_bytes(b"pesi finti")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def opts(self, **kw):
        o = {"repo_dir": STUB, "model_path": str(self.model), "require_gpu": False, "check_deps": False}
        o.update(kw)
        return o

    def ctx(self, scene, options):
        out = self.tmp / "out"
        out.mkdir(exist_ok=True)
        prog = []
        c = StepContext(store=None, job_id="j", step={"id": "s", "payload": {"scene": scene}},
                        params={"options": options}, tmp_dir=out, progress=prog.append,
                        worker_name="t", models_cache=self.tmp / "cache")
        c.prog = prog
        return c

    # ---- plan ----
    def test_plan_variants_and_errors(self):
        a = LingBotMapAdapter()
        steps = a.plan(["https://x/y/video.MP4?dl=1", "https://x/foto.zip", {"id": "k", "images": "/data/f"},
                        {"video": "/v.mov"}], {}, {})
        self.assertEqual([s["id"] for s in steps], ["step_001", "step_002", "step_003", "step_004"])
        self.assertIn("video", steps[0]["payload"]["scene"])
        self.assertIn("images", steps[1]["payload"]["scene"])
        self.assertEqual(steps[3]["payload"]["scene"]["id"], "scena4")
        for bad in ([{"id": "x"}], [{"video": "a", "images": "b"}], [42]):
            with self.assertRaises(ValueError):
                a.plan(bad, {}, {})

    # ---- esecuzione ----
    def test_run_images_folder(self):
        folder = make_images(self.tmp / "foto", 5)
        ctx = self.ctx({"id": "casa", "images": str(folder)}, self.opts(mode="windowed", keyframe_interval=2))
        ad = LingBotMapAdapter()
        ad.run_step(ctx)
        out = ctx.tmp_dir
        names = {p.name for p in out.iterdir()}
        self.assertTrue({"predictions.npz", "camera_poses.json", "summary.json", "results.jsonl", "run.log"} <= names, names)
        self.assertEqual(("scene.glb" in names), HAVE_TRIMESH)
        rec = json.loads((out / "results.jsonl").read_text())
        self.assertTrue(rec["ok"]); self.assertEqual(rec["frames"], 5)
        poses = json.loads((out / "camera_poses.json").read_text())["frames"]
        self.assertEqual(len(poses), 5)
        self.assertAlmostEqual(poses[4]["position"][0], 2.0)       # w2c t=-0.5*i  =>  centro camera x=+0.5*i
        self.assertEqual(ctx.prog[-1], 1.0)
        self.assertTrue(all(b >= a for a, b in zip(ctx.prog, ctx.prog[1:])), f"progresso non monotono: {ctx.prog}")
        self.assertTrue(ad.validate(ctx))
        self.assertEqual(ad.last_metrics, {"ok": 1, "failed": 0})
        log = (out / "run.log").read_text()
        self.assertIn("--mode windowed", log)
        self.assertIn("--keyframe_interval 2", log)

    def test_viewer_replaced_not_imported(self):
        """Il vero lingbot_map.vis importa viser: il runner non deve mai eseguirlo."""
        folder = make_images(self.tmp / "foto", 3)
        ctx = self.ctx({"id": "a", "images": str(folder)}, self.opts())
        LingBotMapAdapter().run_step(ctx)
        self.assertNotIn("viser non installato", (ctx.tmp_dir / "run.log").read_text())

    def test_glb_failure_is_not_fatal(self):
        if HAVE_TRIMESH:
            self.skipTest("trimesh presente: il GLB riesce")
        folder = make_images(self.tmp / "foto", 3)
        ctx = self.ctx({"id": "a", "images": str(folder)}, self.opts())
        LingBotMapAdapter().run_step(ctx)
        s = json.loads((ctx.tmp_dir / "summary.json").read_text())
        self.assertFalse(s["glb"]); self.assertIn("trimesh", s["glb_error"])
        self.assertTrue((ctx.tmp_dir / "predictions.npz").exists())

    def test_video_url_and_zip_url(self):
        srv_dir = self.tmp / "srv"; srv_dir.mkdir()
        (srv_dir / "clip.mp4").write_bytes(b"fake video")
        make_zip(srv_dir / "foto.zip", 6)
        srv = FileServer(srv_dir)
        try:
            c1 = self.ctx({"id": "v", "video": srv.base + "/clip.mp4"}, self.opts(fps=7))
            LingBotMapAdapter().run_step(c1)
            self.assertIn("--fps 7", (c1.tmp_dir / "run.log").read_text())
            self.assertEqual(json.loads((c1.tmp_dir / "results.jsonl").read_text())["frames"], 12)
            shutil.rmtree(self.tmp / "out")
            c2 = self.ctx({"id": "z", "images": srv.base + "/foto.zip"}, self.opts())
            LingBotMapAdapter().run_step(c2)
            self.assertEqual(json.loads((c2.tmp_dir / "results.jsonl").read_text())["frames"], 6)
        finally:
            srv.stop()

    def test_download_limit_and_bad_inputs(self):
        srv_dir = self.tmp / "srv"; srv_dir.mkdir()
        (srv_dir / "big.mp4").write_bytes(b"x" * 3_000_000)
        srv = FileServer(srv_dir)
        try:
            c = self.ctx({"id": "v", "video": srv.base + "/big.mp4"}, self.opts(max_download_gb=0.001))
            with self.assertRaisesRegex(RuntimeError, "limite"):
                LingBotMapAdapter().run_step(c)
            c = self.ctx({"id": "v", "video": srv.base + "/manca.mp4"}, self.opts())
            with self.assertRaises(Exception):
                LingBotMapAdapter().run_step(c)
        finally:
            srv.stop()
        for scene, pat in (({"id": "a", "images": str(self.tmp / "nessuna")}, "non trovata"),
                           ({"id": "a", "video": "/no/video.mp4"}, "non trovato")):
            with self.assertRaisesRegex(RuntimeError, pat):
                LingBotMapAdapter().run_step(self.ctx(scene, self.opts()))
        one = make_images(self.tmp / "una", 1)
        with self.assertRaisesRegex(RuntimeError, "almeno 2 foto"):
            LingBotMapAdapter().run_step(self.ctx({"id": "a", "images": str(one)}, self.opts()))

    def test_zip_slip_rejected(self):
        z = self.tmp / "evil.zip"
        make_zip(z, 3, evil=True)
        with self.assertRaisesRegex(RuntimeError, "non sicuro"):
            _safe_unzip(z, self.tmp / "dest")
        self.assertFalse((self.tmp / "evil.txt").exists())

    def test_zip_with_single_subfolder_and_macosx(self):
        z = self.tmp / "ok.zip"
        with zipfile.ZipFile(z, "w") as zf:
            for i in range(3):
                zf.writestr(f"album/{i}.png", b"p")
            zf.writestr("__MACOSX/album/._0.png", b"junk")
        c = self.ctx({"id": "a", "images": str(_safe_unzip(z, self.tmp / "d"))}, self.opts())
        LingBotMapAdapter().run_step(c)
        self.assertEqual(json.loads((c.tmp_dir / "results.jsonl").read_text())["frames"], 3)

    def test_errors_are_explained(self):
        folder = make_images(self.tmp / "foto", 3)
        c = self.ctx({"id": "a", "images": str(folder)}, self.opts(first_k=13))       # il finto demo simula un OOM
        with self.assertRaises(RuntimeError) as cm:
            LingBotMapAdapter().run_step(c)
        msg = str(cm.exception)
        self.assertIn("CUDA out of memory", msg); self.assertIn("Suggerimento", msg)
        with self.assertRaisesRegex(RuntimeError, "model_path non trovato"):
            LingBotMapAdapter().run_step(self.ctx({"id": "a", "images": str(folder)},
                                                  self.opts(model_path=str(self.tmp / "no.pt"))))
        with self.assertRaisesRegex(RuntimeError, "repo_dir non valido"):
            LingBotMapAdapter().run_step(self.ctx({"id": "a", "images": str(folder)}, self.opts(repo_dir=str(self.tmp))))
        with self.assertRaisesRegex(ValueError, "mode non valido"):
            LingBotMapAdapter().run_step(self.ctx({"id": "a", "images": str(folder)}, self.opts(mode="xx")))

    def test_gpu_required_by_default(self):
        folder = make_images(self.tmp / "foto", 3)
        o = self.opts(); o.pop("require_gpu")
        try:
            import torch
            has = torch.cuda.is_available()
        except ImportError:
            self.skipTest("torch non installato: il controllo GPU richiede torch")
        if has:
            self.skipTest("questa macchina ha una GPU")
        with self.assertRaisesRegex(RuntimeError, "GPU"):
            LingBotMapAdapter().run_step(self.ctx({"id": "a", "images": str(folder)}, o))

    def test_missing_torch_is_reported(self):
        import importlib.util
        if importlib.util.find_spec("torch") is not None:
            self.skipTest("torch installato")
        folder = make_images(self.tmp / "foto", 3)
        o = self.opts(); o.pop("check_deps")
        with self.assertRaisesRegex(RuntimeError, "PyTorch"):
            LingBotMapAdapter().run_step(self.ctx({"id": "a", "images": str(folder)}, o))


class PoolIntegrationTest(unittest.TestCase):
    """Router + worker solo-API + (poi) nodo GPU: il job 3D aspetta la GPU, poi i file si scaricano."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="lb_pool_"))
        cls.model = cls.tmp / "fake.pt"; cls.model.write_bytes(b"w")
        cfg = dict(token=TOKEN, store_root=str(cls.tmp / "store"), lease_timeout=8, handoff_threshold=0.7,
                   session_limit_s=3600, max_attempts=1, default_vram_gb=14, heartbeat_interval=1)
        cls.store = open_store(cfg)
        cls.router = Router(cfg, cls.store)
        cls.srv = make_server(cls.router, "127.0.0.1", 0)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.wd = Watchdog(cls.router, 0.5); cls.wd.start()
        cls.api = Worker(cls.base, TOKEN, cls.store, name="local-api", api_only=True, hb=1, poll=0.2, verbose=False)
        threading.Thread(target=cls.api.run, daemon=True).start()
        cls.gpu = None
        time.sleep(0.5)

    @classmethod
    def tearDownClass(cls):
        cls.api.dead = True
        if cls.gpu:
            cls.gpu.dead = True
        cls.wd.stop(); cls.srv.shutdown(); cls.srv.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def call(self, method, path, body=None, **kw):
        return call_http(self.base, method, path, body, token=kw.pop("token", TOKEN), **kw)

    def wait(self, jid, timeout=40):
        t0 = time.time()
        while time.time() - t0 < timeout:
            code, s = self.call("GET", f"/jobs/{jid}")
            if s["status"] in ("completed", "failed"):
                return s
            time.sleep(0.2)
        self.fail(f"job {jid} non terminato: {s}")

    def options(self, **kw):
        o = {"repo_dir": STUB, "model_path": str(self.model), "require_gpu": False, "check_deps": False}
        o.update(kw)
        return {"options": o, "title": "3D test"}

    def test_1_waits_for_gpu_then_runs_and_downloads(self):
        folder = make_images(self.tmp / "foto", 5)
        code, r = self.call("POST", "/jobs/plan", {"adapter": "lingbot_map",
                                                   "items": [{"id": "casa", "images": str(folder)}],
                                                   "params": self.options()})
        self.assertEqual(code, 200, r)
        jid = r["job_id"]
        time.sleep(1.5)
        code, s = self.call("GET", f"/jobs/{jid}")
        self.assertEqual(set(s["steps"].values()), {"pending"}, "il worker solo-API non deve prendere job GPU")
        code, st = self.call("GET", "/status")
        self.assertGreaterEqual(st["demand"]["nodes_needed"], 1, "il launcher deve vedere che serve una GPU")
        # arriva un nodo GPU
        type(self).gpu = Worker(self.base, TOKEN, self.store, name="kaggle-gpu", hb=1, poll=0.2, verbose=False)
        threading.Thread(target=self.gpu.run, daemon=True).start()
        self.assertEqual(self.wait(jid)["status"], "completed")

        code, res = self.call("GET", f"/jobs/{jid}/results")
        self.assertEqual(res["ok"], 1, res)
        self.assertIn("Ricostruzione 3D completata: 5 fotogrammi", res["items"][0]["text"])
        names = {f["name"] for f in res["files"]}
        self.assertTrue({"predictions.npz", "camera_poses.json", "summary.json", "run.log"} <= names, names)
        self.assertNotIn("results.jsonl", names)
        self.assertEqual(("scene.glb" in names), HAVE_TRIMESH)

        f = next(f for f in res["files"] if f["name"] == "camera_poses.json")
        import urllib.request
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(f"{self.base}/jobs/{jid}/download?step={f['step']}&name=camera_poses.json",
                                     headers={"X-Pool-Token": TOKEN})
        with opener.open(req) as resp:
            body = resp.read()
            self.assertEqual(resp.headers["Content-Length"], str(len(body)))
            self.assertIn("attachment", resp.headers["Content-Disposition"])
        self.assertEqual(len(json.loads(body)["frames"]), 5)
        self.assertEqual(len(body), f["size"])
        type(self).done_job, type(self).done_step = jid, f["step"]

    def test_2_download_is_protected(self):
        jid, step = self.done_job, self.done_step
        base = f"/jobs/{jid}/download"
        self.assertEqual(self.call("GET", f"{base}?step={step}&name=summary.json", token="x")[0], 401)
        self.assertEqual(self.call("GET", f"{base}?step={step}")[0], 400)
        for evil in ("../../../../etc/passwd", "../manifest.json", "/etc/passwd", "..%2F..%2Fmanifest.json",
                     "results.jsonl", "nonesiste.bin", "."):
            code, r = self.call("GET", f"{base}?step={step}&name={evil}")
            self.assertEqual(code, 404, f"{evil!r} -> {code} {r}")
        self.assertEqual(self.call("GET", f"{base}?step=step_999&name=summary.json")[0], 404)
        self.assertEqual(self.call("GET", f"/jobs/nope/download?step=a&name=b")[0], 404)

    def test_3_bad_inputs_are_400_and_failures_explained(self):
        code, r = self.call("POST", "/jobs/plan", {"adapter": "lingbot_map", "items": [{"id": "x"}], "params": {}})
        self.assertEqual(code, 400); self.assertIn("video", r["error"])
        folder = make_images(self.tmp / "foto2", 3)
        code, r = self.call("POST", "/jobs/plan", {"adapter": "lingbot_map",
                                                   "items": [{"id": "boom", "images": str(folder)}],
                                                   "params": self.options(first_k=13)})
        self.assertEqual(code, 200)
        self.assertEqual(self.wait(r["job_id"])["status"], "failed")
        code, res = self.call("GET", f"/jobs/{r['job_id']}/results")
        self.assertTrue(any("CUDA out of memory" in e for e in res["errors"]), res["errors"])

    def test_4_webapp_has_3d_mode(self):
        code, page = self.call("GET", "/", token=None)
        for needle in ('data-mode="3d"', "form-3d", "lingbot_map", "downloadFile"):
            self.assertIn(needle, page)


@unittest.skipUnless(os.environ.get("LINGBOT_MAP_DIR"), "imposta LINGBOT_MAP_DIR per provare il repo vero (serve GPU)")
class RealRepoTest(unittest.TestCase):
    def test_real_model_on_gpu(self):
        tmp = Path(tempfile.mkdtemp(prefix="lb_real_"))
        folder = tmp / "foto"
        folder.mkdir()
        src = os.environ.get("LINGBOT_MAP_IMAGES")
        if not src:
            self.skipTest("imposta LINGBOT_MAP_IMAGES con una cartella di foto (es. example/courthouse)")
        shutil.copytree(src, folder, dirs_exist_ok=True)
        out = tmp / "out"; out.mkdir()
        c = StepContext(None, "j", {"id": "s", "payload": {"scene": {"id": "r", "images": str(folder)}}},
                        {"options": {"first_k": 20}}, out, lambda p: None, "t", tmp / "cache")
        LingBotMapAdapter().run_step(c)
        self.assertTrue((out / "predictions.npz").exists())


class RecipeMemoryTest(unittest.TestCase):
    def test_recipe_has_light_fallback_for_small_gpus(self):
        from core.recipes import lingbot_cfg
        cfg = lingbot_cfg({"video": "x"}, {})
        run = cfg["run"]
        self.assertIn(") || (", run)                                   # secondo tentativo automatico se la GPU finisce la memoria
        first, second = run.split(") || (")
        self.assertIn("--keyframe_interval 4", first); self.assertIn("--max-frames 150", first)
        self.assertIn("--keyframe_interval 8", second); self.assertIn("--num_scale_frames 2", second)
        self.assertIn("--max-frames 60", second)
        self.assertIn("expandable_segments:True", first)
        self.assertEqual(cfg["input_key"], "video")
        self.assertNotIn(") || (", lingbot_cfg({"video": "x"}, {"no_fallback": True})["run"])
        user = lingbot_cfg({"video": "x"}, {"keyframe_interval": 2})["run"]   # la scelta dell'utente vince
        self.assertEqual(user.count("--keyframe_interval 2"), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)