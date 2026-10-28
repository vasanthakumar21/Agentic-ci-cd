"""Reusable test harness for the agentic-cicd-generator (task 21, design §11.8).

Houses the snapshot harness (:mod:`tests.harness.snapshot`) that renders a target
plugin's output for a fixture IR via the in-memory ``plan()`` path (writing
nothing) and asserts it matches a committed snapshot. Adding a new plugin (the
§11.9 matrix) gets regression coverage for free: drop a fixture, record a snapshot
once, and the snapshot guards the plugin thereafter.
"""
