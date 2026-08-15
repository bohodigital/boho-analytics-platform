"""Private bulk-lake package.

Submodules are intentionally not imported here: the lake lock is POSIX-only, while the ordinary
analytics CLI and library remain importable on every supported platform.
"""
