"""Print saved CI logs as GitHub error annotations, so failures are readable from the
checks API (annotations) without downloading the raw job log."""
import glob
import os
import subprocess
import sys


def emit(title: str, text: str, limit: int = 3500) -> None:
    text = text.strip()[-limit:] or "(empty)"
    enc = text.replace("%", "%25").replace("\r", "").replace("\n", "%0A")
    print(f"::error title={title}::{enc}")


for path in sorted(glob.glob(os.path.join(os.environ.get("RUNNER_TEMP", "/tmp"), "ci-*.log"))):
    emit(os.path.basename(path), open(path, encoding="utf-8", errors="replace").read())
r = subprocess.run(["bash", "scripts/proxy-log.sh", "cache"], capture_output=True, text=True)
if (r.stdout + r.stderr).strip():
    emit("squid cache.log", r.stdout + r.stderr, 2500)
r = subprocess.run(["bash", "scripts/proxy-log.sh", "40"], capture_output=True, text=True)
if (r.stdout + r.stderr).strip():
    emit("squid access.log", r.stdout + r.stderr, 2500)
for svc in ("egress-proxy", "llama-swap"):
    r = subprocess.run(["docker", "compose", "logs", "--no-color", "--tail", "60", svc],
                       capture_output=True, text=True, cwd=sys.argv[1] if len(sys.argv) > 1 else ".")
    if (r.stdout + r.stderr).strip():
        emit(f"docker compose logs {svc}", r.stdout + r.stderr, 2500)
