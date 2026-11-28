"""Avvia il server GPU Pool dentro l'app Android (Chaquopy). Gira in un thread dedicato."""
import os
import runpy
import sys


def start(app_dir, token, port):
    os.environ["GPOOL_TOKEN"] = token
    os.environ["GPOOL_PORT"] = str(port)
    os.chdir(app_dir)
    if app_dir not in sys.path:
        sys.path.insert(0, app_dir)
    sys.argv = ["run_webapp.py", "--no-browser", "--port", str(port)]
    runpy.run_path(os.path.join(app_dir, "run_webapp.py"), run_name="__main__")
