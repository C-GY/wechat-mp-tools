"""Keep the observed payload separate from the merged local download cache."""

import copy
import time


def capture_metadata(feed, task_id=""):
    return {"rpa_payload": copy.deepcopy(feed), "collected_at": time.time(), "capture_task_id": task_id}
