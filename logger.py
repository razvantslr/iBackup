import logging

def logger_init(verbose):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s]: %(message)s"
    )
    return logging.getLogger("iBackup")