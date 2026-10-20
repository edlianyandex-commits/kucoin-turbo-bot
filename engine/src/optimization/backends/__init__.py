"""Optimizer backends removed from this build; the module stays as a stub.

Live trading, backtests and the panel do not import anything from here.
If an optimizer is ever needed again, restore the backend modules and their
entries in BACKEND_RUNNERS.
"""


def get_backend_runner(name: str):
    raise ValueError(f"optimizer backend {name!r} is not available in this build")
