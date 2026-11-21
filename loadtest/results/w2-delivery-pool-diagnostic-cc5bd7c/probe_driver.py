"""Run the original paired workload with a measurement-only delivery worker hook."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

home = Path(__file__).resolve().parent
result = home / "m4-pair/loadtest/results/w2-delivery-pool-diagnostic-cc5bd7c"
if len(sys.argv) == 1:
    result.mkdir(exist_ok=False)
    for arm in ("before", "after"):
        repo = home / f"w2-{arm}"
        subprocess.run([sys.executable, "-u", __file__, arm], cwd=repo, check=True)
    stats = [json.loads(next((result / arm).glob("m11-*/summary.json")).read_text()) for arm in ("before", "after")]
    assert stats[0]["workload_sha256"] == stats[1]["workload_sha256"]
    assert stats[0]["workload_sha256"].startswith("5804a2c0")
    raise SystemExit(0)

arm = sys.argv[1]
repo = home / f"w2-{arm}"
output = result / arm
module_file = repo / "loadtest/run_saturation.py"
spec = importlib.util.spec_from_file_location("run_saturation", module_file)
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)
write_json = harness.write_json

def configure_probe(path, data):
    if path.name == "compose.measurement.json":
        worker = data["services"]["delivery-worker"]
        worker.update(environment={"PYTHONPATH": "/w0-pool-probe"}, volumes=[
            f"{home / 'w2-pool-probe'}:/w0-pool-probe:ro",
            f"{output}:/w0-pool-output",
        ])
    write_json(path, data)

harness.write_json = configure_probe
os.environ.update(ASSIST_ENABLED="0", OPENAI_API_KEY="", ANTHROPIC_API_KEY="")
for key in ("DELIVERY_BATCH", "DELIVERY_CONCURRENCY"):
    os.environ.pop(key, None)
args = argparse.Namespace(output=output, rates=[700], keys=[1000], workers=[4], duration=60,
                          critical_rate=2, delivery_workers=1, ingest_workers=2, drain_seconds=20,
                          image_project=f"w0w2-{arm}", max_in_flight=2000)
print("pool diagnostic", arm, flush=True)
sys.argv.extend(["--offer-shape", "700 normal + 2 critical, 1000 keys, 4 normal workers, 2 ingest, 1 delivery, 60s + 20s drain"])
rc = harness.main(args)
assert (output / "delivery-pool.json").is_file(), "missing graceful shutdown measurements"
raise SystemExit(rc)
