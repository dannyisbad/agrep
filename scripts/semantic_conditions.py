"""Record a quiet-start precondition; never discard a completed sample."""
import json
import os
from pathlib import Path
import subprocess
import time


def wait_for_load(output, maximum):
    waited = 0.0
    path = Path(output).with_suffix('.waits.jsonl')
    while maximum is not None and os.getloadavg()[0] > maximum:
        record = dict(time=time.time(), load=os.getloadavg(), maximum=maximum,
                      uptime=subprocess.check_output(['uptime'], text=True).strip())
        with path.open('a') as f:
            f.write(json.dumps(record)+'\n')
        start=time.perf_counter()
        time.sleep(5)
        waited += time.perf_counter()-start
    return waited
