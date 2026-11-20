"""Wake idle workers on SIGTERM and let their current unit of work finish."""

import asyncio
import signal
from contextlib import contextmanager


@contextmanager
def termination_event():
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    previous = signal.getsignal(signal.SIGTERM)
    loop.add_signal_handler(signal.SIGTERM, stop.set)
    try:
        yield stop
    finally:
        loop.remove_signal_handler(signal.SIGTERM)
        signal.signal(signal.SIGTERM, previous)


async def wait_or_stop(stop, seconds):
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass
