import os
import sys

# Make the api/ modules importable as top-level packages (graph, parsing, ...)
sys.path.insert(0, os.path.dirname(__file__))

# Unit tests run without the ~1.5 GB ML stack; guard behaviour is tested with a
# stand-in classifier (tests/test_guard.py) and, where llm-guard is installed
# (the Docker image), against the real model.
os.environ.setdefault("GUARD_MODE", "off")
