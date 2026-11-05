from __future__ import annotations

import subprocess
import sys
from pathlib import Path

lock_path = Path(sys.argv[1])
expected = {
    line.strip()
    for line in lock_path.read_text(encoding="utf-8").splitlines()
    if line.strip() and not line.lstrip().startswith("#")
}
if not expected:
    raise SystemExit("requirements.txt is empty; run scripts/lock-vllm.ps1 first")

completed = subprocess.run(
    [sys.executable, "-m", "pip", "freeze", "--all"],
    check=True,
    capture_output=True,
    text=True,
)
actual = {line.strip() for line in completed.stdout.splitlines() if line.strip()}
if actual != expected:
    missing = sorted(actual - expected)
    extra = sorted(expected - actual)
    print("Python dependency lock does not match the pinned vLLM base image.")
    if missing:
        print("Missing from requirements.txt:")
        print("\n".join(missing))
    if extra:
        print("Not present in base image:")
        print("\n".join(extra))
    raise SystemExit(1)
print(f"Verified {len(actual)} pinned Python distributions.")
