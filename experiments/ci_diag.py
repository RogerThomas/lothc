"""Temporary CI diagnostic (to be removed): how long a 0.02s sleep takes."""

import sys
import time

samples = []
for _ in range(30):
    started = time.perf_counter()
    time.sleep(0.02)
    samples.append(time.perf_counter() - started)
samples.sort()
print(f"{sys.argv[1]}: sleep(0.02) median {samples[15]:.4f} max {samples[-1]:.4f}")
