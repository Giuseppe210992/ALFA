#!/usr/bin/env bash
# Prepara su Kaggle l'ambiente per il Gaussian Splatting: COLMAP + nerfstudio in un Python 3.10 isolato.
# (nerfstudio non si installa sul Python 3.13 di Kaggle: si usa un ambiente separato con uv.)
set -u
say() { echo "[splat] $*"; }
fail() { say "ERRORE: $*"; exit 1; }
export DEBIAN_FRONTEND=noninteractive
VENV=${GPOOL_NSVENV:-/tmp/nsvenv}

say "1/5 COLMAP e ffmpeg (apt)"
(apt-get update -qq && apt-get install -y -qq colmap ffmpeg) > /tmp/apt.log 2>&1 || { tail -5 /tmp/apt.log; fail "apt-get non è riuscito a installare COLMAP"; }
command -v colmap > /dev/null || fail "COLMAP non risulta installato"
say "colmap: $(colmap -h 2>&1 | head -1)"

say "2/5 Python 3.10 isolato (uv)"
pip install -q uv > /dev/null 2>&1 || fail "uv non si è installato"
uv venv --python 3.10 "$VENV" > /tmp/uv.log 2>&1 || { tail -5 /tmp/uv.log; fail "uv non è riuscito a creare Python 3.10"; }
PY="$VENV/bin/python"

say "3/5 PyTorch (CUDA 12.1)"
uv pip install --python "$PY" torch==2.4.1 torchvision==0.19.1 --index-url https://download.pytorch.org/whl/cu121 > /tmp/torch.log 2>&1 \
  || { tail -5 /tmp/torch.log; fail "PyTorch non si è installato"; }

say "4/5 nerfstudio (qualche minuto)"
uv pip install --python "$PY" "numpy<2" ninja nerfstudio > /tmp/ns.log 2>&1 || { tail -8 /tmp/ns.log; fail "nerfstudio non si è installato"; }

say "5/5 gsplat precompilato (se esiste) e controllo finale"
uv pip install --python "$PY" --reinstall --no-deps gsplat --index-url https://docs.gsplat.studio/whl/pt24cu121 > /tmp/gsplat.log 2>&1 \
  && say "gsplat precompilato installato" || say "gsplat precompilato non disponibile: verrà compilato alla prima esecuzione (qualche minuto)"
"$PY" -c "import torch; print('[splat] torch', torch.__version__, 'cuda', torch.cuda.is_available())" || fail "torch non parte"
"$VENV/bin/ns-train" --help > /dev/null 2>&1 || fail "ns-train non parte"
command -v nvcc > /dev/null && say "nvcc presente" || say "nvcc assente (ok se gsplat è precompilato)"
say "ambiente pronto"
