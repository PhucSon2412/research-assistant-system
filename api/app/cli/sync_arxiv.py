"""One bounded discovery pass, same runtime boundary as direct/Celery.

No endpoint or gate override. --force skips only the successful poll schedule.
Exit 0: completed/bound/disabled; 1: error/halted; 3: busy; 2: bad configuration.
"""
import argparse
import sys

from app.config import get_settings
from app.db import get_engine
from app.discovery import run_sync


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m app.cli.sync_arxiv')
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args(argv)
    try:
        settings = get_settings()
        if settings.db is None:
            raise ValueError()
        engine = get_engine()
    except Exception:
        print('discovery_runtime_configuration_required', file=sys.stderr)
        return 2
    counts = run_sync(engine, settings.discovery, force=args.force)
    print('Discovery: ' + ', '.join(f'{k}={v}' for k, v in sorted(counts.items())))
    if counts['busy']:
        return 3
    return 1 if any(k.startswith(('halted:', 'error:')) for k in counts) else 0


if __name__ == '__main__':
    sys.exit(main())
