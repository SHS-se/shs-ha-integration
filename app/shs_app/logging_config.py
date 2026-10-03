"""Supervisor log verbosity, independent of control and allocation tracing."""
import logging

LEVELS = {name: getattr(logging, name.upper()) for name in ('debug', 'info', 'warning', 'error', 'critical')}


def configure_logging(level):
    if not isinstance(level, str) or level not in LEVELS:
        raise ValueError('log_level must be one of: ' + ', '.join(LEVELS))
    logging.basicConfig(level=LEVELS[level], format='%(asctime)s %(levelname)s [%(name)s] %(message)s')
    logging.getLogger().setLevel(LEVELS[level])
    # Dashboard polling is useful at Debug, but obscures runtime messages at Info.
    logging.getLogger('aiohttp.access').setLevel(logging.INFO if level == 'debug' else max(logging.WARNING, LEVELS[level]))

    logging.getLogger('shs_core.controller').setLevel(logging.INFO if level == 'debug' else max(logging.WARNING, LEVELS[level]))
