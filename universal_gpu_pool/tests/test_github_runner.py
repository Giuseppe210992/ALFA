"""Test di «Programma da GitHub»: analisi del repository, collegamento Kaggle, esecuzione, risultati.

Nessuna rete e nessun account reale: un finto GitHub (con repository git veri), un finto Kaggle
(stesso protocollo dell'SDK, esegue DAVVERO lo script pubblicato) e un finto provider IA.
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.base_adapter import StepContext  # noqa: E402
from adapters.github_repo_adapter import GitHubRepoAdapter  # noqa: E402
from core import github_repo as gh, remote_api_backend as rab, repo_planner as rp, secrets_store  # noqa: E402
from core.aggregator_api import Router, make_server  # noqa: E402
from core.kaggle_client import KaggleClient, KaggleError, slugify  # noqa: E402
from core.kaggle_script import build_script  # noqa: E402
from core.state_store import open_store  # noqa: E402
from core.watchdog import Watchdog  # noqa: E402
from tests.fake_github import FakeGitHub, make_repo  # noqa: E402
from tests.fake_kaggle import FakeKaggle  # noqa: E402
from tests.fake_openai import FakeOpenAI  # noqa: E402
from tests.repo_fixtures import info_from_dir  # noqa: E402
from tests.test_system import http as call_http  # noqa: E402
from worker.worker_daemon import Worker  # noqa: E402

TOKEN = "gh-test-token"
PLAN = {
    "title": "Demo App", "summary": "Ripete un testo e lo salva in un file.", "gpu": False,
    "setup": ["echo installazione-ok"],
    "run": "python app.py --text {testo} --repeat {ripetizioni} --out {OUT}",
    "inputs": [{"key": "testo", "label": "Testo da ripetere", "type": "text", "required": True},
               {"key": "ripetizioni", "label": "Quante volte", "type": "number", "default": "2", "required": False}],
    "outputs": [], "timeout_min": 5,
}


class EnvCase(unittest.TestCase):
    """Imposta i falsi servizi tramite le variabili d'ambiente previste per i test."""

    @classmethod
    def setUpClass(cls):
        cls.env_backup = dict(os.environ)
        cls.tmp = Path(tempfile.mkdtemp(prefix="ghr_"))
        make_repo(cls.tmp / "gh", "o", "demo")
        cls.github = FakeGitHub(cls.tmp / "gh").start()
        cls.kaggle = FakeKaggle(queue_s=0.2).start()
        os.environ.update(cls.github.env())
        os.environ["GPOOL_KAGGLE_API"] = cls.kaggle.base
        for k in ("KAGGLE_API_TOKEN", "KAGGLE_USERNAME", "KAGGLE_KEY", "DEMO_FAIL"):
            os.environ.pop(k, None)

    @classmethod
    def tearDownClass(cls):
        cls.github.stop(); cls.kaggle.stop()
        os.environ.clear(); os.environ.update(cls.env_backup)
        shutil.rmtree(cls.tmp, ignore_errors=True)


# ---------------------------------------------------------------------------------------------
class ParseAndPlanTest(unittest.TestCase):
    def test_parse_url_variants(self):
        ok = {
            "https://github.com/Robbyant/lingbot-map": ("Robbyant", "lingbot-map", None),
            "https://github.com/o/r.git": ("o", "r", None),
            "github.com/o/r/": ("o", "r", None),
            "https://www.github.com/o/r/tree/dev/sub/dir": ("o", "r", "dev"),
            "git@github.com:o/r.git": ("o", "r", None),
            "o/r": ("o", "r", None),
            "  <https://github.com/o/r?tab=readme>  ": ("o", "r", None),
        }
        for url, exp in ok.items():
            self.assertEqual(gh.parse_url(url), exp, url)
        for bad in ("", "   ", "https://gitlab.com/o/r", "https://github.com/solo", "ciao", "https://github.com/o/../x"):
            with self.assertRaises(gh.RepoError, msg=bad):
                gh.parse_url(bad)

    def test_normalize_plan_sanitizes(self):
        p = rp.normalize_plan({
            "title": "T" * 500, "run": "python x.py --a {a} --b {zzz}", "setup": "pip install x\n# commento\n\nlibreria",
            "gpu": 0, "timeout_min": 99999,
            "inputs": [{"key": "A", "type": "file", "label": "Foto"},                   # chiave minuscola, tipo file -> url
                       {"key": "a", "type": "text"},                                    # duplicato dopo minuscolo
                       {"key": "bad key!", "type": "text"}, {"key": "7x"},              # invalide
                       {"key": "mode", "type": "choice", "choices": []},                # choice senza scelte -> text
                       "non-un-dict"],
            "outputs": ["results/*.png", "/etc/passwd", "../fuori", "~/x"],
            "warnings": ["w1", "", None]})
        self.assertEqual(len(p["title"]), 100)
        self.assertEqual(p["setup"], ["pip install x", "libreria"])
        self.assertEqual([(i["key"], i["type"]) for i in p["inputs"]], [("a", "url"), ("mode", "text")])
        self.assertEqual(p["outputs"], ["results/*.png"])
        self.assertEqual(p["timeout_min"], 540)
        self.assertFalse(p["gpu"])
        self.assertTrue(any("zzz" in w for w in p["warnings"]), p["warnings"])
        with self.assertRaises(ValueError):
            rp.normalize_plan("testo")
        w = rp.normalize_plan({"run": "python app.py --share"})["warnings"]
        self.assertTrue(any("sito web" in x for x in w))

    # Risposta VERA di un modello (qwen3.8-27b via Groq/FreeLLMAPI, 27/11/2026) al prompt di analisi di un repository.
    REAL_LLM_ANSWER = ('{\n "title": "FaceBlur",\n "summary": "Questo programma prende un video e sfoca automaticamente tutti i volti.",\n '
                       '"gpu": true,\n "setup": ["pip install -r requirements.txt"],\n '
                       '"run": "python blur_video.py --input {video_url} --output {OUT}/output.mp4 --strength {strength}",\n '
                       '"inputs": [{"key": "video_url", "label": "Link al video", "help": "Link diretto al file video.", "type": "url", '
                       '"default": "", "required": true, "choices": []},\n '
                       '{"key": "strength", "label": "Intensità sfocatura", "help": "Da 1 a 100.", "type": "number", '
                       '"default": "25", "required": true, "choices": []}],\n "outputs": ["{OUT}/output.mp4"],\n '
                       '"timeout_min": 60,\n "warnings": ["Il modello (150 MB) viene scaricato al primo avvio."]}')

    def test_real_llm_answer_is_usable(self):
        plan = rp.normalize_plan(rp._extract_json(self.REAL_LLM_ANSWER))
        self.assertEqual([(i["key"], i["type"]) for i in plan["inputs"]], [("video_url", "url"), ("strength", "number")])
        self.assertEqual(plan["outputs"], [], "un segnaposto non è un percorso: va scartato")
        self.assertEqual(plan["warnings"], ["Il modello (150 MB) viene scaricato al primo avvio."])
        vals = rp.validate_values(plan, {"video_url": "https://x.org/a.mp4"})
        self.assertEqual(vals["strength"], "25")
        cmd = rp.render(plan["run"], {**vals, "video_url": "/tmp/in/video_url.mp4"})
        self.assertEqual(cmd, "python blur_video.py --input /tmp/in/video_url.mp4 --output /kaggle/working/outputs/output.mp4 --strength 25")

    def test_render_quotes_everything(self):
        cmd = rp.render("python a.py --t {t} --f {file} --o {OUT} {sconosciuto}",
                        {"t": "x; rm -rf / `id` $(id) 'q'", "file": "/in/a b.mp4"}, out_dir="/out dir")
        import shlex
        toks = shlex.split(cmd)
        self.assertEqual(toks, ["python", "a.py", "--t", "x; rm -rf / `id` $(id) 'q'", "--f", "/in/a b.mp4",
                                "--o", "/out dir", "{sconosciuto}"])

    def test_validate_values(self):
        plan = rp.normalize_plan({**PLAN, "inputs": PLAN["inputs"] + [
            {"key": "modo", "type": "choice", "choices": ["a", "b"], "default": "a", "required": False},
            {"key": "foto", "type": "url", "required": False}, {"key": "ok", "type": "bool", "required": False}]})
        v = rp.validate_values(plan, {"testo": " ciao ", "ok": "sì"})
        self.assertEqual(v, {"testo": "ciao", "ripetizioni": "2", "modo": "a", "foto": "", "ok": "1"})
        for bad, msg in (({}, "Testo da ripetere"), ({"testo": "x", "ripetizioni": "tre"}, "numero"),
                         ({"testo": "x", "modo": "z"}, "scegli"), ({"testo": "x", "foto": "file.png"}, "link che inizia")):
            with self.assertRaisesRegex(ValueError, msg):
                rp.validate_values(plan, bad)

    def test_extract_json_variants(self):
        j = {"a": 1, "b": {"c": "}"}}
        for text in (json.dumps(j), "```json\n" + json.dumps(j) + "\n```", "Ecco il piano:\n" + json.dumps(j) + "\nFine."):
            self.assertEqual(rp._extract_json(text), j)
        for bad in ("niente json", '{"a": 1'):
            with self.assertRaises(ValueError):
                rp._extract_json(bad)

    def test_heuristic_plan_demo_repo(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            make_repo(tmp, "o", "demo")
            info = info_from_dir(tmp / "o" / "demo.git", "o", "demo", "Programma di prova")
            plan = rp.heuristic_plan(info)
            self.assertEqual(plan["setup"], ["pip install -q -r requirements.txt"])
            self.assertEqual(plan["run"], "python app.py --text ciao --repeat 3 --out results")
            self.assertEqual(plan["source"], "semplice")
            # senza esempi nel README: argparse
            info["readme"] = "Solo testo."
            plan = rp.heuristic_plan(info)
            self.assertEqual(plan["run"], "python app.py --text {text}")
            self.assertEqual(plan["inputs"][0]["key"], "text")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_heuristic_placeholders_in_readme_command(self):
        info = {"owner": "o", "repo": "r", "ref": "main", "url": "u", "description": "", "tree": ["demo.py"],
                "readme": "```bash\npython demo.py --model_path /path/to/ck.pt --image_folder path/to/rgb --mask_sky\n```",
                "files": {"demo.py": "import argparse"}, "license": "", "stars": 0, "size_kb": 0, "language": "", "archived": False}
        plan = rp.heuristic_plan(info)
        self.assertEqual(plan["run"], "python demo.py --model_path {model_path} --image_folder {image_folder} --mask_sky")
        self.assertEqual([i["key"] for i in plan["inputs"]], ["model_path", "image_folder"])
        self.assertTrue(all(i["type"] == "url" for i in plan["inputs"]))

    @unittest.skipUnless(os.environ.get("LINGBOT_REPO_DIR"), "imposta LINGBOT_REPO_DIR con un clone di lingbot-map")
    def test_heuristic_on_real_repo(self):
        info = info_from_dir(os.environ["LINGBOT_REPO_DIR"], "Robbyant", "lingbot-map")
        plan = rp.heuristic_plan(info)
        self.assertIn("demo.py", plan["run"])


class MakePlanTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = FakeOpenAI().start()
        cls.tmp = Path(tempfile.mkdtemp())
        make_repo(cls.tmp, "o", "demo")
        cls.info = info_from_dir(cls.tmp / "o" / "demo.git", "o", "demo", "Programma di prova")
        cls.env_backup = dict(os.environ)

    @classmethod
    def tearDownClass(cls):
        cls.fake.stop(); shutil.rmtree(cls.tmp, ignore_errors=True)
        os.environ.clear(); os.environ.update(cls.env_backup)

    def backend(self, key):
        os.environ["T_PLAN_KEY"] = key
        p = rab.Provider("t", self.fake.base_url, "m", "T_PLAN_KEY", rpm=100, rpd=100)
        return rab.RemoteAPIBackend([p], cache_dir=tempfile.mkdtemp(), state_file=Path(tempfile.mkdtemp()) / "u.json",
                                    total_timeout=10, request_timeout=5)

    def test_llm_plan_used(self):
        self.fake.plan_json = "Certo! ```json\n" + json.dumps(PLAN) + "\n```"
        plan = rp.make_plan(self.info, self.backend("plan"))
        self.assertEqual(plan["source"], "ia")
        self.assertEqual(plan["run"], PLAN["run"])
        self.assertEqual([i["key"] for i in plan["inputs"]], ["testo", "ripetizioni"])
        prompt = self.fake.calls[-1]["body"]["messages"][-1]["content"]
        self.assertIn("python app.py --text", prompt)            # README e file chiave arrivano all'IA
        self.assertIn("requirements.txt", prompt)

    def test_bad_llm_answer_falls_back(self):
        self.fake.plan_json = "non riesco, scusa"
        plan = rp.make_plan(self.info, self.backend("plan"))
        self.assertEqual(plan["source"], "semplice")
        self.assertIn("analisi semplice", plan["note"])
        self.fake.plan_json = json.dumps({**PLAN, "run": ""})
        self.assertEqual(rp.make_plan(self.info, self.backend("plan"))["source"], "semplice")

    def test_no_ai_account_uses_rules(self):
        os.environ.pop("T_PLAN_KEY", None)
        p = rab.Provider("t", self.fake.base_url, "m", "T_PLAN_KEY")
        be = rab.RemoteAPIBackend([p], cache_dir=tempfile.mkdtemp(), state_file=Path(tempfile.mkdtemp()) / "u.json")
        plan = rp.make_plan(self.info, be)
        self.assertEqual(plan["source"], "semplice")
        self.assertIn("Collega", plan["note"])


# ---------------------------------------------------------------------------------------------
class KaggleClientTest(EnvCase):
    def test_slugify(self):
        self.assertEqual(slugify("GPool a1b2c3d4"), "gpool-a1b2c3d4")
        self.assertEqual(slugify("  Mio  Titolo!! "), "mio-titolo")

    def test_account_and_quota(self):
        c = KaggleClient(token="good-token")
        self.assertEqual(c.whoami(), "tester")
        q = c.quota()
        self.assertEqual((q["gpu_total_h"], q["gpu_used_h"], q["gpu_left_h"]), (30.0, 2.0, 28.0))
        auth = [a for p, a in self.kaggle.calls if "IntrospectToken" in p][-1]
        self.assertEqual(auth, "Bearer good-token")

    def test_bad_token_messages(self):
        with self.assertRaisesRegex(KaggleError, "non valido|scaduto"):
            KaggleClient(token="cattivo").whoami()
        with self.assertRaises(KaggleError) as cm:
            KaggleClient(token="cattivo").call("kernels.KernelsApiService", "GetAcceleratorQuotaStatistics")
        self.assertIn("non ti riconosce", str(cm.exception))
        with self.assertRaisesRegex(KaggleError, "non collegato"):
            KaggleClient()

    def test_phone_verification_error_is_explained(self):
        k = FakeKaggle(no_phone=True).start()
        try:
            c = KaggleClient(token="good-token", base=k.base)
            with self.assertRaises(KaggleError) as cm:
                c.push_script("GPool abc12345", "print(1)")
            self.assertIn("telefono", str(cm.exception))
        finally:
            k.stop()

    def test_push_wait_list_download_delete(self):
        c = KaggleClient(token="good-token")
        script = ("import os\nw=os.environ['GPOOL_WORKDIR']\nfor i in range(7):\n"
                  "    os.makedirs(w+'/outputs/d',exist_ok=True); open(w+'/outputs/d/f%d.txt'%i,'w').write(str(i))\nprint('ciao')\n")
        user, slug = c.push_script("GPool prova1", script)
        self.assertEqual((user, slug), ("tester", "gpool-prova1"))
        seen = []
        st, msg = c.wait(user, slug, 30, on_status=seen.append, poll=0.1)
        self.assertEqual(st, "COMPLETE")
        self.assertIn("QUEUED", seen); self.assertEqual(seen[-1], "COMPLETE")
        files, log = c.list_output(user, slug)
        self.assertEqual(len(files), 7, "paginazione: la pagina del finto Kaggle ne ha 4")
        self.assertIn("ciao", log)
        dest = Path(tempfile.mkdtemp()) / "x" / "f3.txt"
        c.download(next(f["url"] for f in files if f["name"].endswith("f3.txt")), dest)
        self.assertEqual(dest.read_text(), "3")
        c.delete(user, slug)
        with self.assertRaises(KaggleError):
            c.status(user, slug)

    def test_script_error_status(self):
        c = KaggleClient(token="good-token")
        user, slug = c.push_script("GPool errore1", "raise SystemExit(5)")
        st, msg = c.wait(user, slug, 30, poll=0.1)
        self.assertEqual(st, "ERROR"); self.assertIn("5", msg)

    def test_wait_timeout(self):
        c = KaggleClient(token="good-token")
        user, slug = c.push_script("GPool lento1", "import time; time.sleep(30)")
        with self.assertRaisesRegex(KaggleError, "non ha finito"):
            c.wait(user, slug, 0.5, poll=0.1)


# ---------------------------------------------------------------------------------------------
class AdapterTest(EnvCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp(prefix="ghr_out_"))
        os.environ["KAGGLE_API_TOKEN"] = "good-token"
        os.environ["GPOOL_FETCH_BACKOFF"] = "0.1"
        os.environ.pop("DEMO_FAIL", None)

    def tearDown(self):
        shutil.rmtree(self.out, ignore_errors=True)
        os.environ.pop("KAGGLE_API_TOKEN", None); os.environ.pop("DEMO_FAIL", None); os.environ.pop("GPOOL_FETCH_BACKOFF", None)

    def params(self, **kw):
        p = {"repo_url": "https://github.com/o/demo", "plan": dict(PLAN), "values": {"testo": "ciao mondo", "ripetizioni": "3"},
             "options": {"poll_s": 0.2}}
        p.update(kw)
        return p

    def run_adapter(self, params):
        ad = GitHubRepoAdapter()
        step = ad.plan([], params, {})[0]
        prog = []
        ctx = StepContext(None, "j", step, params, self.out, prog.append, "t", self.out / "c")
        ad.run_step(ctx)
        rec = json.loads((self.out / "results.jsonl").read_text().splitlines()[0])
        return ad, rec, prog

    def test_plan_validation(self):
        ad = GitHubRepoAdapter()
        for bad, msg in ((dict(self.params(), repo_url=""), "repo_url"), (dict(self.params(), repo_url="https://x.com/a"), "GitHub"),
                         (dict(self.params(), plan={"title": "x"}), "comando"),
                         (dict(self.params(), values={}), "Testo da ripetere")):
            with self.assertRaisesRegex(ValueError, msg):
                ad.plan([], bad, {})
        step = ad.plan([], self.params(), {})[0]
        cfg = step["payload"]["cfg"]
        self.assertEqual(cfg["values"], {"testo": "ciao mondo", "ripetizioni": "3"})
        self.assertEqual(cfg["repo_url"], "https://github.com/o/demo")

    def test_success_end_to_end(self):
        n0 = len(self.kaggle.calls)
        ad, rec, prog = self.run_adapter(self.params())
        self.assertTrue(rec["ok"], rec)
        self.assertIn("Completato", rec["text"])
        self.assertIn("outputs/result.txt", rec["files"])
        self.assertEqual((self.out / "outputs" / "result.txt").read_text(), "ciao mondo\nciao mondo\nciao mondo\n")
        log = (self.out / "run.log").read_text()
        for needle in ("git clone", "installazione-ok", "python app.py"):
            self.assertIn(needle, log)
        self.assertEqual(json.loads((self.out / "result.json").read_text())["commit"][:1].isalnum(), True)
        self.assertEqual(prog[-1], 1.0)
        self.assertTrue(all(b >= a for a, b in zip(prog, prog[1:])), prog)
        self.assertEqual(ad.last_metrics, {"ok": 1, "failed": 0})
        self.assertEqual(self.kaggle.kernels, {}, "il notebook va cancellato a fine lavoro")
        sent = [c for c in self.kaggle.calls[n0:] if "SaveKernel" in c[0]]
        self.assertEqual(len(sent), 1)
        self.assertNotIn("good-token", (self.out / "run.log").read_text(), "il token non deve finire nel registro")

    def test_input_url_download(self):
        from tests.test_lingbot import FileServer
        srvdir = self.tmp / "srv"; srvdir.mkdir(exist_ok=True)
        (srvdir / "dati.txt").write_text("uno\ndue\n")
        fs = FileServer(srvdir); self.addCleanup(fs.stop)
        plan = dict(PLAN, run="python app.py --text {testo} --input {file} --out {OUT}",
                    inputs=[{"key": "testo", "label": "Testo", "type": "text"},
                            {"key": "file", "label": "File", "type": "url"}])
        _, rec, _ = self.run_adapter(self.params(plan=plan, values={"testo": "x", "file": fs.base + "/dati.txt"}))
        self.assertTrue(rec["ok"], rec)
        self.assertIn("righe nel file di input: 2", (self.out / "outputs" / "result.txt").read_text())

    # ---- download dell'input da siti problematici (come il «Broken pipe» visto con Google Drive) ----
    def _serve(self, behavior):
        import socketserver, http.server
        data = b"uno\ndue\ntre\n"
        state = {"n": 0, "uas": []}

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                state["n"] += 1
                ua = self.headers.get("User-Agent", "")
                state["uas"].append(ua)
                if behavior == "reset_twice" and state["n"] <= 2:
                    self.connection.close(); return                      # connessione chiusa di colpo
                if behavior == "only_curl" and not ua.startswith("curl"):
                    self.connection.close(); return
                body = (b"<!DOCTYPE html><html><body>Accedi per continuare</body></html>" if behavior == "html" else data)
                self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
                self.wfile.write(body)
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close()))
        return f"http://127.0.0.1:{srv.server_address[1]}/dati.txt", state

    def _run_with_input(self, url):
        plan = dict(PLAN, run="python app.py --text {testo} --input {file} --out {OUT}", setup=[],
                    inputs=[{"key": "testo", "label": "Testo", "type": "text"}, {"key": "file", "label": "File", "type": "url"}])
        return self.run_adapter(self.params(plan=plan, values={"testo": "x", "file": url}))

    def test_download_retries_after_connection_reset(self):
        url, st = self._serve("reset_twice")
        _, rec, _ = self._run_with_input(url)
        self.assertTrue(rec["ok"], rec)
        self.assertGreaterEqual(st["n"], 3)
        self.assertTrue(st["uas"][-1].startswith("Mozilla/5.0"), "deve presentarsi come un browser")
        self.assertIn("righe nel file di input: 3", (self.out / "outputs" / "result.txt").read_text())

    def test_download_falls_back_to_curl(self):
        import shutil as _sh
        if not _sh.which("curl"):
            self.skipTest("curl non installato")
        url, st = self._serve("only_curl")
        _, rec, _ = self._run_with_input(url)
        self.assertTrue(rec["ok"], rec)
        self.assertIn("provo con curl", (self.out / "run.log").read_text())

    def test_html_page_instead_of_file_is_explained(self):
        url, _ = self._serve("html")
        _, rec, _ = self._run_with_input(url)
        self.assertFalse(rec["ok"])
        self.assertIn("pagina web invece del file", rec["error"]); self.assertIn("Chiunque abbia il link", rec["error"])

    def test_unreachable_site_gives_actionable_error(self):
        _, rec, _ = self._run_with_input("http://127.0.0.1:9/nessuno.txt")
        self.assertFalse(rec["ok"])
        self.assertIn("non riesco a scaricare il file di input", rec["error"]); self.assertIn("Dropbox", rec["error"])

    def test_program_failure_is_reported_not_raised(self):
        os.environ["DEMO_FAIL"] = "1"
        ad, rec, _ = self.run_adapter(self.params())
        self.assertFalse(rec["ok"])
        self.assertIn("terminato con errore", rec["error"]); self.assertIn("fatto", rec["error"])   # coda del registro
        self.assertEqual(ad.last_metrics, {"ok": 0, "failed": 1})
        self.assertTrue(ad.validate(StepContext(None, "j", {}, {}, self.out, lambda p: None, "t")))

    def test_setup_failure(self):
        _, rec, _ = self.run_adapter(self.params(plan=dict(PLAN, setup=["exit 7"])))
        self.assertFalse(rec["ok"]); self.assertIn("installazione è fallita", rec["error"])

    def test_wrong_repo_and_timeout(self):
        _, rec, _ = self.run_adapter(self.params(repo_url="https://github.com/o/inesistente"))
        self.assertFalse(rec["ok"]); self.assertIn("impossibile scaricare", rec["error"])
        plan = dict(PLAN, run="sleep 30", inputs=[], setup=[])
        plan["timeout_min"] = 5
        ad = GitHubRepoAdapter()
        step = ad.plan([], self.params(plan=plan, values={}), {})[0]
        step["payload"]["cfg"]["timeout_s"] = 1          # il programma supera il tempo massimo
        ctx = StepContext(None, "j", step, self.params(plan=plan), self.out, lambda p: None, "t", self.out / "c")
        ad.run_step(ctx)
        rec = json.loads((self.out / "results.jsonl").read_text().splitlines()[0])
        self.assertFalse(rec["ok"]); self.assertIn("tempo massimo", rec["error"])

    def test_no_account_or_bad_token(self):
        os.environ.pop("KAGGLE_API_TOKEN")
        _, rec, _ = self.run_adapter(self.params())
        self.assertFalse(rec["ok"]); self.assertIn("non collegato", rec["error"])
        os.environ["KAGGLE_API_TOKEN"] = "revocato"
        shutil.rmtree(self.out); self.out.mkdir()
        _, rec, _ = self.run_adapter(self.params())
        self.assertFalse(rec["ok"]); self.assertTrue("non valido" in rec["error"] or "non ti riconosce" in rec["error"], rec["error"])

    def test_extra_outputs_collected_from_repo(self):
        """Programmi che scrivono dentro il repository (non in {OUT}) vengono raccolti lo stesso."""
        plan = dict(PLAN, run="python app.py --text {testo} --out risultati_locali", inputs=PLAN["inputs"][:1], setup=[])
        _, rec, _ = self.run_adapter(self.params(plan=plan, values={"testo": "dentro"}))
        self.assertTrue(rec["ok"], rec)
        self.assertIn("outputs/dal_repository/risultati_locali/result.txt", rec["files"])


# ---------------------------------------------------------------------------------------------
class SystemTest(EnvCase):
    """Router + worker locale + finti servizi: ciò che fa la webapp, via HTTP."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        d = cls.tmp
        cls.orig = (rab.PROVIDERS_FILE, secrets_store.SECRETS_FILE)
        rab.PROVIDERS_FILE = d / "providers.json"
        secrets_store.SECRETS_FILE = d / "secrets.env"
        os.environ["GPOOL_API_CACHE"] = str(d / "apicache")
        os.environ["GPU_POOL_API_CACHE"] = str(d / "apicache")
        for p in rab.DEFAULT_PROVIDERS:
            os.environ.pop(p.key_env, None)
        rab.reset_shared_backend()
        cls.fake_llm = FakeOpenAI().start()
        cfg = dict(token=TOKEN, store_root=str(d / "store"), lease_timeout=8, handoff_threshold=0.7, session_limit_s=3600,
                   max_attempts=1, default_vram_gb=14, heartbeat_interval=1)
        cls.store = open_store(cfg)
        cls.router = Router(cfg, cls.store)
        cls.srv = make_server(cls.router, "127.0.0.1", 0)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.wd = Watchdog(cls.router, 0.5); cls.wd.start()
        cls.worker = Worker(cls.base, TOKEN, cls.store, name="local-api", api_only=True, hb=1, poll=0.2, verbose=False)
        threading.Thread(target=cls.worker.run, daemon=True).start()
        time.sleep(0.4)

    @classmethod
    def tearDownClass(cls):
        cls.worker.dead = True; cls.wd.stop(); cls.srv.shutdown(); cls.srv.server_close(); cls.fake_llm.stop()
        rab.PROVIDERS_FILE, secrets_store.SECRETS_FILE = cls.orig
        rab.reset_shared_backend()
        super().tearDownClass()

    def call(self, method, path, body=None, **kw):
        return call_http(self.base, method, path, body, token=kw.pop("token", TOKEN), **kw)

    def wait(self, jid, timeout=60):
        t0 = time.time()
        while time.time() - t0 < timeout:
            code, s = self.call("GET", f"/jobs/{jid}")
            if s["status"] in ("completed", "failed"):
                return s
            time.sleep(0.3)
        self.fail(f"job {jid} non terminato: {s}")

    def test_1_gpu_account_flow(self):
        code, s = self.call("GET", "/gpu")
        self.assertFalse(s["kaggle"]["connected"]); self.assertFalse(s["colab"]["automatable"])
        for bad, msg in (("cattivo", "non valido"), ("due parole", "spazi"), ("{non json", "kaggle.json")):
            code, r = self.call("POST", "/gpu/kaggle", {"token": bad})
            self.assertEqual(code, 400, (bad, r)); self.assertIn(msg, r["error"])
        self.assertFalse(self.call("GET", "/gpu")[1]["kaggle"]["connected"], "un token errato non va salvato")
        code, s = self.call("POST", "/gpu/kaggle", {"token": "  good-token\n"})
        self.assertEqual(code, 200, s)
        k = s["kaggle"]
        self.assertEqual((k["connected"], k["username"], k["quota"]["gpu_left_h"]), (True, "tester", 28.0))
        self.assertNotIn("good-token", json.dumps(s), "il token non deve tornare al browser")
        self.assertIn("KAGGLE_API_TOKEN=good-token", secrets_store.SECRETS_FILE.read_text())
        self.assertTrue(self.call("POST", "/gpu/kaggle/test", {})[1]["kaggle"]["connected"])

    def test_2_analyze_repo_with_ai_then_without(self):
        os.environ["CUSTOM_API_KEY"] = "plan"
        code, s = self.call("GET", "/settings")
        provs = [{k: v for k, v in p.items() if k not in ("has_key", "key_hint", "status")} for p in s["providers"]]
        for p in provs:
            if p["name"] == "custom":
                p.update(base_url=self.fake_llm.base_url, model="m", rpm=1000)
        self.call("POST", "/settings/providers", {"providers": provs})
        self.call("POST", "/settings/key", {"key_env": "CUSTOM_API_KEY", "value": "plan"})
        self.fake_llm.plan_json = json.dumps(PLAN)
        code, r = self.call("POST", "/repos/analyze", {"url": "https://github.com/o/demo"})
        self.assertEqual(code, 200, r)
        self.assertEqual((r["repo"]["owner"], r["repo"]["repo"], r["repo"]["ref"]), ("o", "demo", "main"))
        self.assertEqual(r["plan"]["source"], "ia"); self.assertTrue(r["kaggle_connected"])
        self.assertEqual([i["key"] for i in r["plan"]["inputs"]], ["testo", "ripetizioni"])
        # senza IA: regole semplici
        self.call("POST", "/settings/key", {"key_env": "CUSTOM_API_KEY", "value": ""})
        code, r = self.call("POST", "/repos/analyze", {"url": "o/demo"})
        self.assertEqual((code, r["plan"]["source"]), (200, "semplice"))
        # errori comprensibili
        for url, msg in (("https://github.com/o/nonesiste", "non trovato"), ("pippo", "GitHub"), ("", "Incolla")):
            code, r = self.call("POST", "/repos/analyze", {"url": url})
            self.assertEqual(code, 400, url); self.assertIn(msg, r["error"])

    def test_3_run_job_and_download(self):
        code, r = self.call("POST", "/jobs/plan", {"adapter": "github_repo", "items": [], "params": {
            "repo_url": "https://github.com/o/demo", "plan": PLAN, "values": {"testo": "dal sistema", "ripetizioni": "2"},
            "title": "Demo", "options": {"poll_s": 0.2}}})
        self.assertEqual(code, 200, r)
        jid = r["job_id"]
        code, st = self.call("GET", "/status")
        self.assertEqual(st["demand"]["nodes_needed"], 0, "il lavoro orchestrato da qui non deve chiedere un nodo GPU")
        self.assertEqual(self.wait(jid)["status"], "completed")
        code, res = self.call("GET", f"/jobs/{jid}/results")
        self.assertEqual((res["ok"], res["failed"]), (1, 0), res)
        names = {f["name"] for f in res["files"]}
        self.assertTrue({"outputs/result.txt", "run.log", "result.json"} <= names, names)
        f = next(f for f in res["files"] if f["name"] == "outputs/result.txt")
        import urllib.request
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(f"{self.base}/jobs/{jid}/download?step={f['step']}&name=outputs/result.txt",
                                     headers={"X-Pool-Token": TOKEN})
        with op.open(req) as resp:
            self.assertEqual(resp.read().decode(), "dal sistema\ndal sistema\n")
        code, s = self.call("GET", "/status")
        job = next(j for j in s["jobs"] if j["id"] == jid)
        self.assertEqual((job["items_ok"], job["items_failed"]), (1, 0))

    def test_4_failed_run_shows_log_and_can_retry(self):
        os.environ["DEMO_FAIL"] = "1"
        code, r = self.call("POST", "/jobs/plan", {"adapter": "github_repo", "items": [], "params": {
            "repo_url": "https://github.com/o/demo", "plan": PLAN, "values": {"testo": "x"}, "options": {"poll_s": 0.2}}})
        self.assertEqual(self.wait(r["job_id"])["status"], "completed")
        code, res = self.call("GET", f"/jobs/{r['job_id']}/results")
        self.assertEqual((res["ok"], res["failed"]), (0, 1))
        self.assertIn("terminato con errore", res["items"][0]["error"])
        os.environ.pop("DEMO_FAIL")
        code, r2 = self.call("POST", f"/jobs/{r['job_id']}/retry", {})
        self.assertEqual(code, 200, r2)
        self.assertEqual(self.wait(r2["job_id"])["status"], "completed")
        self.assertEqual(self.call("GET", f"/jobs/{r2['job_id']}/results")[1]["ok"], 1)
        self.assertEqual(self.call("POST", f"/jobs/{r2['job_id']}/retry", {})[0], 400, "già riuscito: niente da rilanciare")

    def test_5_bad_requests(self):
        code, r = self.call("POST", "/jobs/plan", {"adapter": "github_repo", "items": [], "params": {
            "repo_url": "https://github.com/o/demo", "plan": PLAN, "values": {}}})
        self.assertEqual(code, 400); self.assertIn("Testo da ripetere", r["error"])
        code, r = self.call("POST", "/jobs/plan", {"adapter": "github_repo", "items": [], "params": {"repo_url": "x"}})
        self.assertEqual(code, 400)

    def test_6_disconnect(self):
        code, s = self.call("POST", "/gpu/kaggle", {"token": ""})
        self.assertFalse(s["kaggle"]["connected"])
        self.assertNotIn("KAGGLE_API_TOKEN", os.environ)
        code, r = self.call("POST", "/jobs/plan", {"adapter": "github_repo", "items": [], "params": {
            "repo_url": "https://github.com/o/demo", "plan": PLAN, "values": {"testo": "x"}}})
        self.wait(r["job_id"])
        res = self.call("GET", f"/jobs/{r['job_id']}/results")[1]
        self.assertIn("non collegato", res["items"][0]["error"])

    def test_6b_upload_endpoint(self):
        import urllib.request, urllib.error
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def post(path, data, token=TOKEN):
            req = urllib.request.Request(self.base + path, data=data, method="POST",
                                         headers={"X-Pool-Token": token, "Content-Type": "application/octet-stream"})
            try:
                with op.open(req) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())
        code, r = post("/uploads?name=foto%20mie.zip", b"PK-finto-zip" * 1000)
        self.assertEqual(code, 200, r)
        self.assertEqual((r["name"], r["size"]), ("foto mie.zip", 12000))
        from core import uploads
        self.assertEqual(uploads.path_for(r["id"]).stat().st_size, 12000)
        self.assertEqual(post("/uploads?name=a", b"x", token="sbagliato")[0], 401)
        self.assertEqual(post("/uploads?name=a", b"")[0], 400)
        self.router.cfg["max_upload_gb"] = 0.00001                      # 10 KB
        code, r = post("/uploads?name=grande.bin", b"x" * 50_000)
        self.assertEqual(code, 400); self.assertIn("troppo grande", r["error"])
        self.router.cfg.pop("max_upload_gb")
        self.assertEqual(self.call("GET", "/health")[0], 200)

    def test_6c_network_info_for_phone(self):
        code, n = self.call("GET", "/network")
        self.assertEqual(code, 200)
        self.assertFalse(n["lan"], "su 127.0.0.1 il telefono non può collegarsi")
        self.assertIsNone(n["url"]); self.assertGreater(n["port"], 0)
        self.router.bind = ("0.0.0.0", n["port"])                       # come con --lan
        try:
            code, n2 = self.call("GET", "/network")
            self.assertTrue(n2["lan"])
            if n2["ip"]:
                self.assertEqual(n2["url"], f"http://{n2['ip']}:{n2['port']}/")
                self.assertFalse(n2["ip"].startswith(("127.", "169.254.")))
        finally:
            self.router.bind = ("127.0.0.1", n["port"])
        self.assertEqual(self.call("GET", "/network", token="x")[0], 401)

    def test_7_webapp_has_github_and_gpu_ui(self):
        code, page = self.call("GET", "/", token=None)
        for needle in ('data-mode="repo"', "form-repo", "/repos/analyze", "/gpu/kaggle", "kaggle.com/settings/api", "btn-phone", "avvia-telefono.bat", "secret-gen", "GPOOL_TOKEN"):
            self.assertIn(needle, page)


class LinkFixTest(unittest.TestCase):
    def test_share_links_become_direct(self):
        from core.linkfix import direct_url
        fid = "1jdIF5mTjOy8RI6sr1FnXqkNtUsuZMy6D"
        for u in (f"https://drive.google.com/file/d/{fid}/view?usp=sharing", f"https://drive.google.com/open?id={fid}",
                  f"https://drive.google.com/uc?export=download&id={fid}"):
            self.assertEqual(direct_url(u)[0], f"https://drive.usercontent.google.com/download?id={fid}&export=download&confirm=t")
        self.assertEqual(direct_url("https://www.dropbox.com/s/ab/f.zip?dl=0")[0], "https://www.dropbox.com/s/ab/f.zip?dl=1")
        self.assertEqual(direct_url("https://github.com/o/r/blob/main/a.mp4")[0], "https://raw.githubusercontent.com/o/r/main/a.mp4")
        self.assertEqual(direct_url("https://esempio.org/v.mp4"), ("https://esempio.org/v.mp4", None))
        with self.assertRaisesRegex(ValueError, "CARTELLA"):
            direct_url("https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOp")
        with self.assertRaisesRegex(ValueError, "OneDrive"):
            direct_url("https://1drv.ms/u/s!abc")

    def test_url_inputs_are_converted_by_validate_values(self):
        plan = rp.normalize_plan({**PLAN, "inputs": [{"key": "f", "type": "url", "label": "File"}]})
        v = rp.validate_values(plan, {"f": "https://drive.google.com/file/d/ABCDEFGHIJK12/view"})
        self.assertIn("drive.usercontent.google.com/download?id=ABCDEFGHIJK12", v["f"])
        with self.assertRaisesRegex(ValueError, "CARTELLA"):
            rp.validate_values(plan, {"f": "https://drive.google.com/drive/folders/ABCDEFGHIJK12"})


class LingBotOnKaggleTest(EnvCase):
    """La ricostruzione 3D su Kaggle: stesso adapter, runtime «kaggle», script e ricetta pronti."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        stub = ROOT / "tests" / "stub_lingbot"
        files = {str(p.relative_to(stub)): p.read_text(encoding="utf-8") for p in stub.rglob("*.py")}
        make_repo(cls.tmp / "gh", "Robbyant", "lingbot-map", files)
        cls.srv = tempfile.mkdtemp(prefix="lbk_srv_")
        import zipfile
        with zipfile.ZipFile(Path(cls.srv) / "foto.zip", "w") as z:
            for i in range(5):
                z.writestr(f"stanza/{i:03d}.jpg", b"jpg%d" % i)
        from tests.test_lingbot import FileServer
        cls.files = FileServer(cls.srv)
        cls.model = Path(cls.srv) / "fake.pt"
        cls.model.write_bytes(b"w")

    @classmethod
    def tearDownClass(cls):
        cls.files.stop(); shutil.rmtree(cls.srv, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.out = Path(tempfile.mkdtemp(prefix="lbk_out_"))
        os.environ["KAGGLE_API_TOKEN"] = "good-token"

    def tearDown(self):
        shutil.rmtree(self.out, ignore_errors=True)
        os.environ.pop("KAGGLE_API_TOKEN", None)

    def run_adapter(self, items, **opts):
        from adapters.lingbot_map_adapter import LingBotMapAdapter
        params = {"runtime": "kaggle", "options": {"setup": [], "model_path": str(self.model), "poll_s": 0.2, **opts}}
        ad = LingBotMapAdapter()
        step = ad.plan(items, params, {})[0]
        ctx = StepContext(None, "j", step, params, self.out, lambda p: None, "t", self.out / "c")
        ad.run_step(ctx)
        return ad, ctx, json.loads((self.out / "results.jsonl").read_text().splitlines()[0])

    def test_plan_validation_and_link_fix(self):
        from adapters.lingbot_map_adapter import LingBotMapAdapter
        ad = LingBotMapAdapter()
        st = ad.plan([{"video": "https://drive.google.com/file/d/ABCDEFGHIJK12/view"}], {"runtime": "kaggle"}, {})
        cfg = st[0]["payload"]["cfg"]
        self.assertIn("drive.usercontent.google.com/download?id=ABCDEFGHIJK12", cfg["downloads"]["video"])
        self.assertIn("_gpool_prep_images.py {video} /tmp/gpool_frames --fps 5 --max-frames 150", cfg["run"])
        self.assertNotIn("--video_path", cfg["run"], "anche i video passano dalla preparazione dei fotogrammi")
        self.assertIn("--image_folder /tmp/gpool_frames", cfg["run"]); self.assertIn("--use_sdpa", cfg["run"])
        self.assertIn("pip install -q -e .", cfg["setup"][0]); self.assertIn("hf_hub_download", cfg["setup"][1])
        self.assertIn("_gpool_lingbot_runner.py", cfg["files"])
        for bad, msg in (([{"video": "/percorso/locale.mp4"}], "link"), ([{}], "esattamente"), ([], "indica"),
                         ([{"images": "https://drive.google.com/drive/folders/ABCDEFGHIJK12"}], "CARTELLA")):
            with self.assertRaisesRegex(ValueError, msg):
                ad.plan(bad, {"runtime": "kaggle"}, {})

    def test_zip_of_photos_runs_on_kaggle(self):
        ad, ctx, rec = self.run_adapter([{"id": "stanza", "images": self.files.base + "/foto.zip"}])
        self.assertTrue(rec["ok"], rec)
        names = set(rec["files"])
        self.assertTrue({"outputs/predictions.npz", "outputs/camera_poses.json", "outputs/summary.json"} <= names, names)
        poses = json.loads((self.out / "outputs" / "camera_poses.json").read_text())["frames"]
        self.assertEqual(len(poses), 5, "lo ZIP con una cartella di foto va scompattato e passato come cartella")
        self.assertTrue(ad.validate(ctx))
        self.assertIn("--use_sdpa", (self.out / "run.log").read_text())

    def test_awkward_zip_is_prepared_so_the_model_sees_the_photos(self):
        """Il caso reale: foto .jpeg/.PNG in sottocartelle. Senza preparazione il demo vero vede 0 immagini."""
        import zipfile
        with zipfile.ZipFile(Path(self.srv) / "difficile.zip", "w") as z:
            for name in ("album/IMG_1.jpeg", "album/IMG_2.jpeg", "album/viaggio/IMG_3.PNG", "IMG_4.JPEG", "__MACOSX/album/._IMG_1.jpeg", "note.txt"):
                z.writestr(name, b"finto")
        ad, ctx, rec = self.run_adapter([{"id": "d", "images": self.files.base + "/difficile.zip"}])
        self.assertTrue(rec["ok"], rec)
        frames = json.loads((self.out / "outputs" / "camera_poses.json").read_text())["frames"]
        self.assertEqual(len(frames), 4)
        log = (self.out / "run.log").read_text()
        self.assertIn("Foto pronte: 4", log)
        self.assertIn("poche foto (4): num_scale_frames = 3", log)

    def test_live_video_runs_end_to_end_on_kaggle(self):
        """Video registrato sul momento (fps dichiarati sbagliati come nei WebM del browser): il modello deve vedere tutti i fotogrammi."""
        try:
            import cv2, numpy as np
        except ImportError:
            self.skipTest("serve OpenCV")
        p = Path(self.srv) / "live.avi"
        w = cv2.VideoWriter(str(p), cv2.VideoWriter_fourcc(*"MJPG"), 1000, (64, 48))
        if not w.isOpened():
            self.skipTest("OpenCV non scrive AVI qui")
        for i in range(45):
            w.write(np.full((48, 64, 3), i * 5 % 255, np.uint8))
        w.release()
        old_path = os.environ["PATH"]
        os.environ["PATH"] = os.path.dirname(sys.executable) + os.pathsep + old_path      # «python» = lo stesso interprete (ha OpenCV)
        try:
            ad, ctx, rec = self.run_adapter([{"id": "v", "video": self.files.base + "/live.avi"}])
        finally:
            os.environ["PATH"] = old_path
        self.assertTrue(rec["ok"], rec)
        self.assertEqual(len(json.loads((self.out / "outputs" / "camera_poses.json").read_text())["frames"]), 45)
        self.assertIn("Fotogrammi pronti: 45", (self.out / "run.log").read_text())

    def test_zip_without_photos_explains_what_it_contains(self):
        import zipfile
        with zipfile.ZipFile(Path(self.srv) / "vuoto.zip", "w") as z:
            for name in ("dati/a.csv", "dati/b.csv", "leggimi.txt"):
                z.writestr(name, b"x")
        ad, ctx, rec = self.run_adapter([{"id": "d", "images": self.files.base + "/vuoto.zip"}])
        self.assertFalse(rec["ok"])
        self.assertIn("Servono almeno 3 foto", rec["error"]); self.assertIn(".csv", rec["error"])

    def test_failure_is_delivered_and_validates(self):
        os.environ["KAGGLE_API_TOKEN"] = "revocato"
        ad, ctx, rec = self.run_adapter([{"images": self.files.base + "/foto.zip"}])
        self.assertFalse(rec["ok"])
        self.assertTrue(ad.validate(ctx), "un errore va consegnato, non ritentato 5 volte")


class UploadTest(EnvCase):
    """File trascinati dal PC: salvati qui, caricati come dataset privato su Kaggle, trovati dallo script, poi cancellati."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        stub = ROOT / "tests" / "stub_lingbot"
        make_repo(cls.tmp / "gh", "Robbyant", "lingbot-map",
                  {p.relative_to(stub).as_posix(): p.read_text(encoding="utf-8") for p in stub.rglob("*.py")})

    def setUp(self):
        import io as _io
        from core import uploads
        self.uploads, self.io = uploads, _io
        uploads.configure(self.tmp / "up")
        self.out = Path(tempfile.mkdtemp(prefix="up_out_"))
        os.environ["KAGGLE_API_TOKEN"] = "good-token"
        os.environ["GPOOL_FETCH_BACKOFF"] = "0.1"

    def tearDown(self):
        shutil.rmtree(self.out, ignore_errors=True)
        os.environ.pop("KAGGLE_API_TOKEN", None); os.environ.pop("GPOOL_FETCH_BACKOFF", None)

    def save(self, name, data):
        return self.uploads.save_stream(self.io.BytesIO(data), len(data), name, 10**9)

    def make_zip(self, n=5):
        import zipfile
        b = self.io.BytesIO()
        with zipfile.ZipFile(b, "w") as z:
            for i in range(n):
                z.writestr(f"stanza/{i:03d}.jpg", b"jpg%d" % i)
        return b.getvalue()

    # ---- salvataggio sicuro ----
    def test_save_stream_and_safety(self):
        r = self.save("../../etc/passwd", b"ciao")
        self.assertEqual(r["name"], "passwd"); self.assertEqual(r["size"], 4)
        p = self.uploads.path_for(r["id"])
        self.assertEqual(p.read_bytes(), b"ciao")
        self.assertTrue(str(p).startswith(str(self.tmp / "up")))
        self.assertEqual(self.save("a b?*<>.zip", b"x")["name"], "a b____.zip")
        for bad in ("", "../x", "zzzz", "0" * 16, None):
            self.assertIsNone(self.uploads.path_for(bad))
        with self.assertRaisesRegex(self.uploads.UploadError, "vuoto"):
            self.uploads.save_stream(self.io.BytesIO(b""), 0, "a", 10)
        with self.assertRaisesRegex(self.uploads.UploadError, "troppo grande"):
            self.uploads.save_stream(self.io.BytesIO(b"x" * 20), 20, "a", 10)
        before = list((self.tmp / "up").iterdir())
        with self.assertRaisesRegex(self.uploads.UploadError, "interrotto"):
            self.uploads.save_stream(self.io.BytesIO(b"abc"), 10, "a", 100)       # connessione caduta a metà
        self.assertEqual(list((self.tmp / "up").iterdir()), before, "un caricamento interrotto non lascia file")

    # ---- dal PC fino al programma ----
    def run_repo(self, upload_id, extra_values=None):
        plan = dict(PLAN, run="python app.py --text {testo} --input {file} --out {OUT}", setup=[],
                    inputs=[{"key": "testo", "label": "Testo", "type": "text"}, {"key": "file", "label": "File", "type": "url"}])
        params = {"repo_url": "https://github.com/o/demo", "plan": plan, "options": {"poll_s": 0.2},
                  "values": {"testo": "x", "file": "upload:" + upload_id}}
        ad = GitHubRepoAdapter()
        step = ad.plan([], params, {})[0]
        ctx = StepContext(None, "j", step, params, self.out, lambda p: None, "t", self.out / "c")
        ad.run_step(ctx)
        return json.loads((self.out / "results.jsonl").read_text().splitlines()[0])

    def test_uploaded_text_file_reaches_the_program(self):
        up = self.save("dati miei.txt", b"uno\ndue\ntre\nquattro\n")
        n_put = len(self.kaggle.puts)
        rec = self.run_repo(up["id"])
        self.assertTrue(rec["ok"], rec)
        self.assertIn("righe nel file di input: 4", (self.out / "outputs" / "result.txt").read_text())
        self.assertEqual(self.kaggle.puts[n_put:][0][1], len(b"uno\ndue\ntre\nquattro\n"), "byte caricati = byte del file")
        self.assertEqual(self.kaggle.datasets, {}, "il dataset temporaneo va cancellato da Kaggle")
        self.assertEqual(self.kaggle.kernels, {})
        for tokenfree in ((self.out / "run.log").read_text(),):
            self.assertNotIn("good-token", tokenfree)

    def test_plan_rejects_missing_upload(self):
        plan = dict(PLAN, run="python app.py --text {testo} --input {file}", inputs=[
            {"key": "testo", "label": "Testo", "type": "text"}, {"key": "file", "label": "File", "type": "url"}])
        with self.assertRaisesRegex(ValueError, "non è più disponibile"):
            GitHubRepoAdapter().plan([], {"repo_url": "https://github.com/o/demo", "plan": plan,
                                          "values": {"testo": "x", "file": "upload:" + "0" * 16}}, {})

    def test_lost_upload_at_run_time_is_reported(self):
        up = self.save("a.txt", b"x\n")
        plan = dict(PLAN, run="python app.py --text {testo} --input {file}", setup=[], inputs=[
            {"key": "testo", "label": "Testo", "type": "text"}, {"key": "file", "label": "File", "type": "url"}])
        params = {"repo_url": "https://github.com/o/demo", "plan": plan, "values": {"testo": "x", "file": "upload:" + up["id"]}}
        ad = GitHubRepoAdapter()
        step = ad.plan([], params, {})[0]
        shutil.rmtree(self.tmp / "up" / up["id"])
        ctx = StepContext(None, "j", step, params, self.out, lambda p: None, "t", self.out / "c")
        ad.run_step(ctx)
        rec = json.loads((self.out / "results.jsonl").read_text().splitlines()[0])
        self.assertFalse(rec["ok"]); self.assertIn("non è più disponibile", rec["error"])

    def run_lingbot(self):
        from adapters.lingbot_map_adapter import LingBotMapAdapter
        up = self.save("foto.zip", self.make_zip(5))
        params = {"runtime": "kaggle", "options": {"setup": [], "model_path": str(self.tmp / "fake.pt"), "poll_s": 0.2}}
        (self.tmp / "fake.pt").write_bytes(b"w")
        ad = LingBotMapAdapter()
        step = ad.plan([{"id": "s", "images": "upload:" + up["id"]}], params, {})[0]
        self.assertEqual(step["payload"]["cfg"]["downloads"], {})
        ctx = StepContext(None, "j", step, params, self.out, lambda p: None, "t", self.out / "c")
        ad.run_step(ctx)
        return json.loads((self.out / "results.jsonl").read_text().splitlines()[0])

    def test_zip_of_photos_uploaded_and_extracted_by_kaggle(self):
        rec = self.run_lingbot()
        self.assertTrue(rec["ok"], rec)
        poses = json.loads((self.out / "outputs" / "camera_poses.json").read_text())["frames"]
        self.assertEqual(len(poses), 5)
        self.assertEqual(self.kaggle.datasets, {})

    def test_zip_not_extracted_by_kaggle(self):
        k2 = FakeKaggle(unzip=False, queue_s=0.1).start()
        os.environ["GPOOL_KAGGLE_API"] = k2.base
        try:
            rec = self.run_lingbot()
            self.assertTrue(rec["ok"], rec)
            self.assertEqual(len(json.loads((self.out / "outputs" / "camera_poses.json").read_text())["frames"]), 5)
        finally:
            os.environ["GPOOL_KAGGLE_API"] = self.kaggle.base
            k2.stop()

    def test_silent_kaggle_failure_is_retried(self):
        """ERROR immediato senza registro (notebook mai partito): si riprova, poi riesce."""
        k2 = FakeKaggle(silent_fail_first=1).start()
        os.environ["GPOOL_KAGGLE_API"] = k2.base
        os.environ["GPOOL_DATASET_WAIT"] = "0.1"
        try:
            up = self.save("dati.txt", b"uno\ndue\n")
            rec = UploadTest.__dict__["run_repo"](self, up["id"])
            self.assertTrue(rec["ok"], rec)
            self.assertEqual(k2.saves, 2)
        finally:
            os.environ["GPOOL_KAGGLE_API"] = self.kaggle.base
            os.environ.pop("GPOOL_DATASET_WAIT", None)
            k2.stop()

    def test_silent_kaggle_failure_gives_actionable_message(self):
        k2 = FakeKaggle(silent_fail_first=9).start()
        os.environ["GPOOL_KAGGLE_API"] = k2.base
        os.environ["GPOOL_DATASET_WAIT"] = "0.05"
        try:
            up = self.save("dati.txt", b"uno\ndue\n")
            rec = UploadTest.__dict__["run_repo"](self, up["id"])
            self.assertFalse(rec["ok"])
            self.assertEqual(k2.saves, 3)
            self.assertIn("non è nemmeno partito", rec["error"]); self.assertIn("kaggle.com/code", rec["error"])
            self.assertTrue(k2.kernels, "il notebook va lasciato nell'account per leggerne il registro")
        finally:
            os.environ["GPOOL_KAGGLE_API"] = self.kaggle.base
            os.environ.pop("GPOOL_DATASET_WAIT", None)
            k2.stop()

    def test_dataset_status_denied_still_works(self):
        """Caso reale (telefono): il token crea il dataset ma non può leggerne lo stato (403 su GetDatasetStatus).
        Non deve essere un errore: si attende e si riprova ad agganciare il dataset al notebook."""
        k2 = FakeKaggle(deny_status=True, strict_ready=True, dataset_delay=1.0).start()
        os.environ["GPOOL_KAGGLE_API"] = k2.base
        os.environ["GPOOL_DATASET_WAIT"] = "0.3"
        try:
            up = self.save("dati.txt", b"uno\ndue\n")
            rec = UploadTest.__dict__["run_repo"](self, up["id"])
            self.assertTrue(rec["ok"], rec)
            self.assertGreaterEqual(getattr(k2, "invalid_tries", 0), 1, "doveva riprovare almeno una volta")
        finally:
            os.environ["GPOOL_KAGGLE_API"] = self.kaggle.base
            os.environ.pop("GPOOL_DATASET_WAIT", None)
            k2.stop()

    def test_403_on_dataset_is_not_blamed_on_the_phone(self):
        """Il caso reale: 403 sulle chiamate dei dataset. Non va attribuito alla verifica del telefono."""
        k2 = FakeKaggle(deny_datasets=True).start()
        os.environ["GPOOL_KAGGLE_API"] = k2.base
        try:
            up = self.save("dati.txt", b"uno\ndue\n")
            rec = UploadTest.__dict__["run_repo"](self, up["id"])
            self.assertFalse(rec["ok"])
            self.assertIn("creare un dataset", rec["error"]); self.assertIn("StartBlobUpload", rec["error"])
            self.assertIn("Permission", rec["error"]); self.assertIn("LINK", rec["error"])
            self.assertNotIn("telefono", rec["error"])
            self.assertEqual(k2.kernels, {}, "senza il file non si deve nemmeno lanciare il notebook")
            c = KaggleClient(token="good-token", base=k2.base)
            with self.assertRaises(KaggleError) as cm:
                c.call("kernels.KernelsApiService", "SaveKernel", {})        # 400 del finto, non 403: nessuna frase sul telefono
            self.assertNotIn("telefono", str(cm.exception))
        finally:
            os.environ["GPOOL_KAGGLE_API"] = self.kaggle.base
            k2.stop()

    def test_notebook_403_mentions_phone_and_method(self):
        k2 = FakeKaggle(no_phone=True).start()
        try:
            c = KaggleClient(token="good-token", base=k2.base)
            with self.assertRaises(KaggleError) as cm:
                c.push_script("GPool abc12345", "print(1)")
            self.assertIn("telefono", str(cm.exception)); self.assertIn("SaveKernel", str(cm.exception))
            self.assertEqual((cm.exception.code, cm.exception.method), (403, "SaveKernel"))
        finally:
            k2.stop()

    def test_kaggle_rejecting_the_file_cleans_up(self):
        k2 = FakeKaggle(dataset_delay=0.1).start()
        os.environ["GPOOL_KAGGLE_API"] = k2.base
        try:
            c = KaggleClient(token="good-token", base=k2.base)
            p = self.tmp / "x.txt"; p.write_text("hola")
            tok = c.upload_blob(p)
            with self.assertRaisesRegex(KaggleError, "400|rifiutato|elaborare"):
                c.create_dataset("GPool prova", [tok + "-sbagliato"])
            self.assertEqual(k2.datasets, {})
            tok2 = c.upload_blob(p, progress=lambda n: None)
            user, slug = c.create_dataset("GPool prova", [tok2])
            self.assertIn((user, slug), k2.datasets)
            c.delete_dataset(user, slug)
            self.assertEqual(k2.datasets, {})
        finally:
            os.environ["GPOOL_KAGGLE_API"] = self.kaggle.base
            k2.stop()


@unittest.skipUnless(os.environ.get("KAGGLE_API_TOKEN_REAL"), "imposta KAGGLE_API_TOKEN_REAL per provare con Kaggle vero")
class RealKaggleTest(unittest.TestCase):
    def test_account(self):
        c = KaggleClient(token=os.environ["KAGGLE_API_TOKEN_REAL"])
        self.assertTrue(c.whoami())


if __name__ == "__main__":
    unittest.main(verbosity=2)
