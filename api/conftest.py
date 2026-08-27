import os
import sys

# Make the api/ modules importable as top-level packages (graph, parsing, ...)
sys.path.insert(0, os.path.dirname(__file__))
