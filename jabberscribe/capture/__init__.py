"""Capture adapters: optional recorders that feed the drop folder.

The pipeline itself never records. Each adapter here turns some external
source of recordings into drop pairs that honour the contract in spec v2 §5,
so the watcher cannot tell them apart from any other recorder.
"""
