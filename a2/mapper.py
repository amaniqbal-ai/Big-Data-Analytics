#!/usr/bin/env python3
import sys

# Q0 Logic: Count every line
for line in sys.stdin:
    line = line.strip()
    if not line or line.startswith("Timestamp"):
        continue  # Skip header
    # Emit a constant key and the value 1
    print("total_measurements\t1")
