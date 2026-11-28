#!/usr/bin/env python3
import json
import os
import sys
import time
from pathlib import Path


def main():
    json.load(sys.stdin)
    generation = sys.argv[-2]
    parent = os.getppid()
    for _ in range(500):
        if os.getppid() != parent or Path(f"release-{generation}").exists():
            break
        time.sleep(0.02)


if __name__ == "__main__":
    main()
