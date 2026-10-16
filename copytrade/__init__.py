"""Copy-trading wallet tracker: follow whale wallets, enter before the copy-trader crowd, sell into the first pump.

Launcher entry point:  copytrade.start(stop_event=None, background=False)
"""


def start(*args, **kwargs):
    from .engine import start as _start

    return _start(*args, **kwargs)
