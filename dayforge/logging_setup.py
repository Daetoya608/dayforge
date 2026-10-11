import logging


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # aiogram пишет каждый апдейт на INFO — слишком шумно.
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
