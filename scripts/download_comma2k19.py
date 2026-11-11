"""Download the official comma2k19 release; rerun to resume."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from roadwatch.comma_download import main

if __name__ == "__main__":
    main()
