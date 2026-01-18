import logging

def logger_init(verbose):
    # root logger level (all libs)
    logging.basicConfig(
        level=logging.INFO,   # <-- CHEIA
        format="%(asctime)s [%(levelname)s] %(filename)s:%(lineno)d: %(message)s"
    )

    # app log level (only iBackup)
    logger = logging.getLogger("iBackup")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)

    #return logging.getLogger("iBackup")