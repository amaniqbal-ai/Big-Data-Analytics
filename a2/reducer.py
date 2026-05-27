#!/usr/bin/env python3
import sys

current_count = 0

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    key, value = line.split('\t', 1)
    current_count += int(value)

# Output the final total for Q0
print(f"Total Measurements:\t{current_count}")
