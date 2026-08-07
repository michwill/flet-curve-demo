"""The test suite.

A package rather than loose modules for one reason: `test_zaps` reuses
`test_actions`' fake provider, and a package makes that import say where it
comes from -- to pytest, to mypy, and to anyone reading it.
"""
