import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Placeholder smoke test; requires env vars.
if not os.getenv("MONGODB_URI") or not os.getenv("GITHUB_TOKEN"):
    print("skip: env not set")
    sys.exit(0)
print("live smoke not fully implemented")