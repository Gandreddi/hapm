"""Poll the outbox, publish each row to its topic, then mark it published.

A crash between publishing and marking means the row is published again on restart, which is
why delivery is at-least-once and consumers dedupe by event id.
"""

import asyncio
import json
import signal

from aiokafka import AIOKafkaProducer
from aiokafka.admin import AIOKafkaAdminClient, NewTopic
from hapm_core.db import make_engine
from hapm_core.settings import CoreSettings
from hapm_events import fetch_unpublished, mark_published
from hapm_events.envelope import ALL_TOPICS
from hapm_telemetry import configure_logging, get_logger
from sqlalchemy import Engine


class RelaySettings(CoreSettings):
    service_name: str = "relay"
    kafka_bootstrap: str = "localhost:19092"
    poll_interval_seconds: float = 0.5
    batch_size: int = 200


async def ensure_topics(bootstrap: str) -> None:
    admin = AIOKafkaAdminClient(bootstrap_servers=bootstrap)
    await admin.start()
    try:
        existing = set(await admin.list_topics())
        missing = [NewTopic(t, num_partitions=1, replication_factor=1) for t in ALL_TOPICS if t not in existing]
        if missing:
            await admin.create_topics(missing)
    finally:
        await admin.close()


async def relay_once(engine: Engine, producer: AIOKafkaProducer, batch_size: int) -> int:
    with engine.begin() as conn:
        rows = fetch_unpublished(conn, batch_size)
        for event_id, topic, payload in rows:
            key = (payload.get("subject") or event_id).encode()
            await producer.send_and_wait(topic, json.dumps(payload).encode(), key=key)
        mark_published(conn, [r[0] for r in rows])
    return len(rows)


async def run(settings: RelaySettings) -> None:
    configure_logging(settings.service_name, settings.log_level)
    log = get_logger()
    engine = make_engine(settings.database_url)
    await ensure_topics(settings.kafka_bootstrap)
    producer = AIOKafkaProducer(bootstrap_servers=settings.kafka_bootstrap, acks="all")
    await producer.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    log.info("relay started", topics=ALL_TOPICS)
    try:
        while not stop.is_set():
            try:
                published = await relay_once(engine, producer, settings.batch_size)
            except Exception:
                log.exception("relay batch failed; will retry")
                published = 0
            if published:
                log.info("published", count=published)
            else:
                try:
                    await asyncio.wait_for(stop.wait(), settings.poll_interval_seconds)
                except TimeoutError:
                    pass
    finally:
        await producer.stop()


def main() -> None:
    asyncio.run(run(RelaySettings()))


if __name__ == "__main__":
    main()
