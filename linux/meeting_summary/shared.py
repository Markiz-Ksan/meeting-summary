"""Resolve shared source at development time; PyInstaller collects it as modules."""
from pathlib import Path
import sys
source = Path(__file__).resolve().parents[2] / 'src'
if source.is_dir() and str(source) not in sys.path:
    sys.path.insert(0, str(source))
