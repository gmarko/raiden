import gevent
import nest_asyncio
import asyncio  # isort:skip # noqa
from raiden.network.transport.matrix.rtc import aiogevent  # isort:skip # noqa


def setup_asyncio_event_loop() -> None:
    nest_asyncio.apply()
    asyncio.set_event_loop_policy(aiogevent.EventLoopPolicy())  # isort:skip # noqa
    gevent.spawn(asyncio.get_event_loop().run_forever)  # isort:skip # noqa
