"""nflverse loader with an on-disk cache and retries (GitHub downloads occasionally drop)."""
import time

import nflreadpy as nfl

from nflreadpy.config import update_config

update_config(cache_mode="filesystem", cache_duration=3600, verbose=False)


def load(name, *args, tries=5):
    for i in range(tries):
        try:
            return getattr(nfl, name)(*args).to_pandas()
        except ConnectionError:
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))
