# Avvio per uno Space «Gradio» gratuito (il tipo Docker è a pagamento): scarica GPU Pool da GitHub e lo avvia.
# Il repository GitHub deve essere PUBBLICO. Nessun segreto qui dentro.
import io
import os
import shutil
import sys
import urllib.request
import zipfile

REPO = os.environ.get("GPOOL_REPO", "Giuseppe210992/ALFA")
BRANCH = os.environ.get("GPOOL_BRANCH", "claude/new-session-q8o2nc")
HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "gpool_app")


def fetch():
    local = os.environ.get("GPOOL_SRC_DIR")            # solo per prove: cartella già pronta
    if local:
        shutil.copytree(local, APP, dirs_exist_ok=True)
        return
    url = f"https://codeload.github.com/{REPO}/zip/refs/heads/{BRANCH}"
    print("Scarico", url, flush=True)
    data = urllib.request.urlopen(url, timeout=120).read()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        top = z.namelist()[0].split("/")[0]
        prefix = top + "/universal_gpu_pool/"
        for m in z.namelist():
            if m.startswith(prefix) and not m.endswith("/") and "/tests/" not in m:
                dst = os.path.join(APP, m[len(prefix):])
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with z.open(m) as s, open(dst, "wb") as d:
                    shutil.copyfileobj(s, d)


if not os.path.exists(os.path.join(APP, "run_webapp.py")):
    fetch()
os.environ.setdefault("GPOOL_PUBLIC", "1")
os.environ["GPOOL_PORT"] = "7860"
os.chdir(APP)
os.execv(sys.executable, [sys.executable, "run_webapp.py", "--host", "0.0.0.0", "--port", "7860", "--no-browser"])
