import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from hoseo.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
