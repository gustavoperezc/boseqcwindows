"""Entry point for the Bose QC35 II ANC control app."""

from __future__ import annotations

import logging
from pathlib import Path

from app_gui import run_app


def configure_logging() -> None:
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_dir / "bose_qc35_anc.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


if __name__ == "__main__":
    configure_logging()
    run_app()
