"""Supervisor log verbosity, independent of control and allocation tracing."""
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LEVELS = {name: getattr(logging, name.upper()) for name in ('debug', 'info', 'warning', 'error', 'critical')}


def configure_logging(level, directory=None):
    if not isinstance(level, str) or level not in LEVELS:
        raise ValueError('log_level must be one of: ' + ', '.join(LEVELS))
    logging.basicConfig(level=LEVELS[level], format='%(asctime)s %(levelname)s [%(name)s] %(message)s')
    logging.getLogger().setLevel(LEVELS[level])
    if directory is not None:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        path = (directory / 'shs-energy.log').resolve()
        root = logging.getLogger()
        if not any(isinstance(handler, RotatingFileHandler) and handler.baseFilename == str(path.resolve())
                   for handler in root.handlers):
            handler = RotatingFileHandler(path, maxBytes=5*1024*1024, backupCount=3, encoding='utf-8')
            handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s [%(name)s] %(message)s'))
            root.addHandler(handler)
    # Dashboard polling is useful at Debug, but obscures runtime messages at Info.
    logging.getLogger('aiohttp.access').setLevel(logging.INFO if level == 'debug' else max(logging.WARNING, LEVELS[level]))

    logging.getLogger('shs_core.controller').setLevel(logging.INFO if level == 'debug' else max(logging.WARNING, LEVELS[level]))


class ConnectionLog:
    """Keep the first failure and changes, with bounded repeated retry output."""
    def __init__(self, logger):
        self.logger = logger
        self.attempts = 0
        self.last_error = None
        self.online = False

    def attempt(self):
        self.attempts += 1
        if self.attempts == 1:
            self.logger.info('Restoring SHS runtime and connecting to Home Assistant')

    def disconnected(self, error):
        self.online = False
        self.attempts = max(1, self.attempts)
        identity = (type(error).__name__, str(error))
        if identity != self.last_error:
            self.logger.error('Home Assistant connection failed on attempt %s: %s: %s; retrying in 5s',
                self.attempts, *identity, exc_info=(type(error), error, error.__traceback__))
            self.last_error = identity
        elif self.attempts % 12 == 0:
            self.logger.warning('Home Assistant still disconnected after %s attempts: %s: %s; retrying in 5s',
                self.attempts, *identity)

    def connected(self):
        if not self.online:
            self.logger.info('Home Assistant connection active after %s attempts; SHS runtime restored', self.attempts)
            self.online = True
            self.attempts = 0
            self.last_error = None
