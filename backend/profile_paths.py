"""Profile locations without importing business services or opening databases."""
import os
from pathlib import Path
import sys


def persistent_config_dir():
    if sys.platform == 'win32':
        return Path(os.environ.get('APPDATA') or Path.home() / 'AppData' / 'Roaming') / 'Fandow' / 'SelfMediaContentCollector'
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Application Support' / 'SelfMediaContentCollector'
    return Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'self-media-content-collector'
