import sys
from pathlib import Path

# Make the backend package modules (main, secrets_store, lights) importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
