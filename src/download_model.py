#vozmite pzhhhhh
from __future__ import annotations

import logging
import os
from pathlib import Path

from huggingface_hub import snapshot_download

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("model-downloader")


def main() -> None:
    model_id = os.environ.get("MODEL_ID", "Qwen/Qwen2.5-0.5B-Instruct")
    model_dir = Path(os.environ.get("MODEL_DIR", "/models/Qwen2.5-0.5B-Instruct"))
    marker = model_dir / ".download_complete"
    if marker.is_file() and (model_dir / "config.json").is_file():
        logger.info("Model cache is complete; skipping download for %s", model_id)
        return
    model_dir.mkdir(parents=True, exist_ok=True)
    hf_token = os.environ.get("HF_TOKEN") or None
    logger.info("Ensuring model snapshot is available: %s", model_id)
    snapshot_download(repo_id=model_id, local_dir=str(model_dir), token=hf_token)
    marker.write_text("ok\n", encoding="utf-8")
    logger.info("Model snapshot is ready: %s", model_id)


if __name__ == "__main__":
    main()
