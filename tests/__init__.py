"""Tests are a package so a shared double can be imported from anywhere.

Without this the import resolves from the checkout's root and nowhere else,
and a candidate generation -- which is the same tree at a different path --
fails its own validation on it.
"""
