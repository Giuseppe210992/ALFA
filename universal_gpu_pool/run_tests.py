#!/usr/bin/env python3
"""Esegue tutti i controlli automatici: test unitari, test di sistema e le demo originali.

    python run_tests.py          # tutto tranne il test nel browser
    python run_tests.py --e2e    # anche il test end-to-end nel browser (richiede Node + Playwright)
"""
import os, shutil, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)


def main():
    ok = True
    print("== Test unitari e di sistema ==")
    suite = unittest.defaultTestLoader.discover("tests", pattern="test_*.py", top_level_dir=HERE)
    ok &= unittest.TextTestRunner(verbosity=1).run(suite).wasSuccessful()

    for demo in ("run_demo.py", "run_demo_launcher.py"):
        print(f"\n== Demo: {demo} ==")
        r = subprocess.run([sys.executable, demo], capture_output=True, text=True, timeout=300)
        last = (r.stdout.strip().splitlines() or ["(nessun output)"])[-1]
        print(("OK  " if r.returncode == 0 else "ERR ") + last)
        if r.returncode:
            print(r.stderr[-2000:])
        ok &= r.returncode == 0

    if "--e2e" in sys.argv:
        print("\n== Test end-to-end nel browser (copia pulita del progetto) ==")
        tmp = tempfile.mkdtemp(prefix="gpupool_e2e_")
        dst = os.path.join(tmp, "universal_gpu_pool")
        shutil.copytree(HERE, dst, ignore=shutil.ignore_patterns(
            "__pycache__", "gpu_pool_store", "models_cache", "secrets.env", "local_config.yaml",
            "providers.json", "e2e_screens", "e2e_photos", "e2e_gh", "e2e_plan.json", "e2e_foto.zip", "e2e_dati.txt", ".git"))
        r = subprocess.run(["node", "tests/e2e_webapp.js", dst, os.path.join(HERE, "e2e_screens")], cwd=dst)
        ok &= r.returncode == 0
        shutil.rmtree(tmp, ignore_errors=True)

    print("\nRISULTATO:", "TUTTO OK" if ok else "CI SONO ERRORI")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
