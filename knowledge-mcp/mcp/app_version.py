"""Single source of truth for the running application version."""

import os

__version__ = os.getenv("APP_VERSION", "2.2.0")
