"""Explicit migration-time grants; connection comes only from RAS_DB__* settings."""
import argparse
from app.db import get_engine
from app.db_roles import provision


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--recovery", required=True)
    parser.add_argument("--definer", required=True)
    args = parser.parse_args()
    provision(get_engine(), runtime=args.runtime, recovery=args.recovery, definer=args.definer)
    print("auth roles provisioned; safety remains pending")


if __name__ == "__main__":
    main()
