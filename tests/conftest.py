import sys
from pathlib import Path

# Tests import the top-level modules (monitor, gpu_perf, laptop_perf) directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
