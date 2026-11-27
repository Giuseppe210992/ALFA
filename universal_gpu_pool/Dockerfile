# Immagine per Hugging Face Spaces (Docker). Vedi deploy/huggingface/LEGGIMI.md
FROM python:3.11-slim

# Hugging Face esegue il contenitore come utente 1000
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    GPOOL_PUBLIC=1 \
    GPOOL_PORT=7860
WORKDIR $HOME/app

COPY --chown=user . $HOME/app

EXPOSE 7860
CMD ["python", "run_webapp.py", "--host", "0.0.0.0", "--port", "7860", "--no-browser"]
