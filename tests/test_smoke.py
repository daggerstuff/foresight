"""Smoke test — verifies the foresight package is importable."""


def test_package_import():
    import foresight

    assert foresight is not None
