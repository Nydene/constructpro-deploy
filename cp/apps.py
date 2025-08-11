# cp/apps.py
from django.apps import AppConfig
from threading import Thread

class CpConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'cp'

    def ready(self):
        from .embedding_loader import get_glove, get_fasttext

        def preload_embeddings():
            try:
                get_glove()
                get_fasttext()
                print("[INFO] Background preload of embeddings completed.")
            except Exception as e:
                print(f"[WARN] Background preload failed: {e}")

        Thread(target=preload_embeddings).start()
