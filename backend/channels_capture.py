"""Keep the observed payload separate from the merged local download cache."""

import copy
import time


def capture_metadata(feed):
    return {"rpa_payload": copy.deepcopy(feed), "collected_at": time.time()}
