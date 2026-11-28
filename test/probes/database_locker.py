#!/usr/bin/env python3
import sqlite3
import sys


def main():
    connection = sqlite3.connect(sys.argv[1], isolation_level=None)
    connection.execute("PRAGMA busy_timeout=1000")
    connection.execute("BEGIN IMMEDIATE")
    print("locked", flush=True)
    sys.stdin.readline()
    connection.execute("ROLLBACK")


if __name__ == "__main__":
    main()
