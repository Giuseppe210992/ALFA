"""Gaussian Splatting (COLMAP + nerfstudio): ricetta, scelta del motore e programma di esecuzione (con i comandi di nerfstudio finti)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.recipes import SPLAT_REPO, splat_cfg  # noqa: E402

STUB_VENV = ROOT / "tests" / "stub_ns"          # contiene bin/ns-process-data, ns-train, ns-export finti


class SplatRecipeTest(unittest.TestCase):
    def test_recipe(self):
        cfg = splat_cfg({"video": "u"}, {})
        self.assertEqual(cfg["setup"], ["bash _gpool_splat_setup.sh"])
        self.assertIn("_gpool_prep_images.py {video} /tmp/gpool_frames --fps 3", cfg["run"])
        self.assertIn("_gpool_splat_runner.py --images /tmp/gpool_frames --out {OUT} --max-frames 80 --iters 15000", cfg["run"])
        for f in ("_gpool_splat_runner.py", "_gpool_splat_setup.sh", "_gpool_prep_images.py"):
            self.assertIn(f, cfg["files"])
        self.assertIn("uv venv --python 3.10", cfg["files"]["_gpool_splat_setup.sh"])
        self.assertGreaterEqual(cfg["timeout_s"], 7200); self.assertGreaterEqual(cfg["setup_timeout_s"], 3000)
        with self.assertRaises(ValueError):
            splat_cfg({"video": "u"}, {"iters": 5})

    def test_adapter_picks_splat(self):
        from adapters.lingbot_map_adapter import LingBotMapAdapter
        cfg = LingBotMapAdapter().plan([{"video": "https://example.com/a.mp4"}],
                                       {"runtime": "kaggle", "options": {"engine": "splat"}}, {})[0]["payload"]["cfg"]
        self.assertIn("video-to-3dgs", cfg["repo_url"]); self.assertIn("_gpool_splat_runner.py", cfg["run"])
        self.assertGreaterEqual(cfg["setup_timeout_s"], 3000)
        self.assertEqual(SPLAT_REPO, "https://github.com/imcmurray/video-to-3dgs")

    def test_setup_script_is_valid_bash(self):
        r = subprocess.run(["bash", "-n", str(ROOT / "adapters" / "splat_setup.sh")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


class SplatRunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="splat_"))
        self.frames = self.tmp / "frames"; self.frames.mkdir()
        for i in range(30):
            (self.frames / ("%06d.jpg" % (i + 1))).write_bytes(b"x")
        self.out = self.tmp / "out"; self.log = self.tmp / "ns.log"; self.work = self.tmp / "work"

    def run_runner(self, *extra, **env):
        e = dict(os.environ, STUB_NS_LOG=str(self.log), **{k: str(v) for k, v in env.items()})
        return subprocess.run([sys.executable, str(ROOT / "adapters" / "splat_runner.py"), "--images", str(self.frames), "--out", str(self.out),
                               "--venv", str(STUB_VENV), "--work", str(self.work), *extra],
                              capture_output=True, text=True, env=e, timeout=120)

    def test_full_flow(self):
        r = self.run_runner("--max-frames", "12", "--iters", "2000")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.out / "scene_splat.ply").is_file())
        summ = json.loads((self.out / "summary.json").read_text())
        self.assertEqual((summ["frames_used"], summ["frames_registered"], summ["iters"]), (12, 12, 2000))
        poses = json.loads((self.out / "camera_poses.json").read_text())["frames"]
        self.assertEqual(len(poses), 12)
        log = self.log.read_text()
        self.assertIn("--no-gpu --matching-method sequential", log)          # COLMAP senza GPU (la versione di apt è solo CPU)
        self.assertIn("--max-num-iterations 2000", log); self.assertIn("--vis tensorboard", log)
        self.assertIn("gaussian-splat --load-config", log)
        self.assertIn("superspl.at", r.stdout)

    def test_unknown_option_falls_back(self):
        r = self.run_runner("--max-frames", "10", STUB_NS_REJECT_OPTS=1)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        proc = [l for l in self.log.read_text().splitlines() if l.startswith("process")]
        self.assertNotIn("--matching-method", proc[-1]); self.assertIn("--no-gpu", proc[-1])

    def test_colmap_registers_too_few_frames(self):
        r = self.run_runner("--max-frames", "20", STUB_NS_REGISTER=5)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("allineato solo 5 fotogrammi", r.stdout + r.stderr)
        self.assertIn("più lentamente", r.stdout + r.stderr)
        self.assertFalse((self.out / "scene_splat.ply").exists())

    def test_out_of_memory_message(self):
        r = self.run_runner("--max-frames", "10", STUB_NS_OOM=1)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("memoria della GPU è finita", r.stdout + r.stderr)

    def test_too_few_images(self):
        for f in list(self.frames.iterdir())[:25]:
            f.unlink()
        r = self.run_runner()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("almeno 8 immagini", r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
