"""Platform queue abstraction (PB-12, ADR-007).

One clean interface in `sentinel_common/queue.py`:
  publish, consume, ack, nack, dead_letter_count, backlog_count

Supports:
- RabbitMQ (aio-pika) with durable queues and dead-letter queues after 5 failed deliveries
- InMemoryQueue with identical 5-delivery dead-letter semantics for tests
"""

from __future__ import annotations

import asyncio
import json
import uuid
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from platform_telemetry import (
    QUEUE_BACKLOG,
    QUEUE_DEAD_LETTERS,
    QUEUE_MESSAGES_CONSUMED,
    QUEUE_MESSAGES_PUBLISHED,
    extract_queue_context,
    propagation_headers,
)

QUEUE_INVESTIGATIONS = "sentinel.investigations"
QUEUE_ACTIONS = "sentinel.actions"
MAX_DELIVERIES = 5


@dataclass
class QueueMessage:
    message_id: str
    queue: str
    payload: dict[str, Any]
    headers: dict[str, str] = field(default_factory=dict)
    attempts: int = 1
    _raw: Any = None


MessageHandler = Callable[[QueueMessage], Awaitable[None]]


class QueueClient(ABC):
    @abstractmethod
    async def publish(
        self,
        queue: str,
        payload: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
    ) -> str: ...

    @abstractmethod
    async def consume(
        self,
        queue: str,
        handler: MessageHandler,
        *,
        stop_event: asyncio.Event | None = None,
    ) -> None: ...

    @abstractmethod
    async def ack(self, message: QueueMessage) -> None: ...

    @abstractmethod
    async def nack(
        self,
        message: QueueMessage,
        *,
        requeue: bool = True,
        count_attempt: bool = True,
    ) -> None: ...

    @abstractmethod
    async def dead_letter_count(self, queue: str) -> int: ...

    @abstractmethod
    async def backlog_count(self, queue: str) -> int: ...

    @abstractmethod
    async def close(self) -> None: ...


class InMemoryQueue(QueueClient):
    """Exact in-memory implementation of PB-12 queues with DLQ after 5 failed deliveries."""

    def __init__(self, *, service: str = "sentinel", max_deliveries: int = MAX_DELIVERIES) -> None:
        self.service = service
        self.max_deliveries = max_deliveries
        self._queues: dict[str, asyncio.Queue[QueueMessage]] = {}
        self._dlq: dict[str, deque[QueueMessage]] = {}

    def _get_q(self, queue: str) -> asyncio.Queue[QueueMessage]:
        if queue not in self._queues:
            self._queues[queue] = asyncio.Queue()
            self._dlq[queue] = deque()
        return self._queues[queue]

    async def publish(
        self,
        queue: str,
        payload: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
    ) -> str:
        msg_id = uuid.uuid4().hex
        merged_headers = propagation_headers()
        if headers:
            merged_headers.update(headers)
        msg = QueueMessage(
            message_id=msg_id,
            queue=queue,
            payload=dict(payload),
            headers=merged_headers,
            attempts=1,
        )
        q = self._get_q(queue)
        await q.put(msg)
        QUEUE_MESSAGES_PUBLISHED.labels(project="sentinel", service=self.service, queue=queue).inc()
        QUEUE_BACKLOG.labels(project="sentinel", queue=queue).set(q.qsize())
        return msg_id

    async def pop_nowait(self, queue: str) -> QueueMessage | None:
        q = self._get_q(queue)
        try:
            msg = q.get_nowait()
            QUEUE_BACKLOG.labels(project="sentinel", queue=queue).set(q.qsize())
            return msg
        except asyncio.QueueEmpty:
            return None

    async def consume(
        self,
        queue: str,
        handler: MessageHandler,
        *,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        stop = stop_event or asyncio.Event()
        q = self._get_q(queue)
        while not stop.is_set():
            try:
                msg = await asyncio.wait_for(q.get(), timeout=0.2)
            except TimeoutError:
                continue
            QUEUE_BACKLOG.labels(project="sentinel", queue=queue).set(q.qsize())
            extract_queue_context(msg.headers)
            try:
                await handler(msg)
            except Exception:
                await self.nack(msg, requeue=True)

    async def ack(self, message: QueueMessage) -> None:
        QUEUE_MESSAGES_CONSUMED.labels(
            project="sentinel", service=self.service, queue=message.queue, status="acked"
        ).inc()

    async def nack(
        self,
        message: QueueMessage,
        *,
        requeue: bool = True,
        count_attempt: bool = True,
    ) -> None:
        q = self._get_q(message.queue)
        if requeue and not count_attempt:
            await q.put(message)
            QUEUE_BACKLOG.labels(project="sentinel", queue=message.queue).set(q.qsize())
            return
        if requeue and message.attempts < self.max_deliveries:
            message.attempts += 1
            await q.put(message)
            QUEUE_MESSAGES_CONSUMED.labels(
                project="sentinel", service=self.service, queue=message.queue, status="retried"
            ).inc()
        else:
            self._dlq[message.queue].append(message)
            QUEUE_DEAD_LETTERS.labels(project="sentinel", queue=message.queue).set(
                len(self._dlq[message.queue])
            )
            QUEUE_MESSAGES_CONSUMED.labels(
                project="sentinel",
                service=self.service,
                queue=message.queue,
                status="dead_lettered",
            ).inc()
        QUEUE_BACKLOG.labels(project="sentinel", queue=message.queue).set(q.qsize())

    async def dead_letter_count(self, queue: str) -> int:
        return len(self._dlq.get(queue, ()))

    async def backlog_count(self, queue: str) -> int:
        return self._get_q(queue).qsize()

    async def close(self) -> None:
        return None


class RabbitMQQueue(QueueClient):
    """RabbitMQ implementation using aio-pika with durable queues & DLX after 5 failed deliveries."""

    def __init__(
        self, url: str, *, service: str = "sentinel", max_deliveries: int = MAX_DELIVERIES
    ) -> None:
        self.url = url
        self.service = service
        self.max_deliveries = max_deliveries
        self._conn: Any = None
        self._channel: Any = None
        self._declared: set[str] = set()

    async def _ensure_channel(self) -> Any:
        if self._channel is None or self._channel.is_closed:
            import aio_pika

            self._conn = await aio_pika.connect_robust(self.url)
            self._channel = await self._conn.channel()
            # One message at a time per worker (Architecture p.32)
            await self._channel.set_qos(prefetch_count=1)
        return self._channel

    async def _declare_queue(self, queue: str) -> Any:
        channel = await self._ensure_channel()
        dlq_name = f"{queue}.dlq"
        if queue not in self._declared:
            await channel.declare_queue(dlq_name, durable=True)
            await channel.declare_queue(
                queue,
                durable=True,
                arguments={
                    "x-dead-letter-exchange": "",
                    "x-dead-letter-routing-key": dlq_name,
                },
            )
            self._declared.add(queue)
        return await channel.get_queue(queue)

    async def publish(
        self,
        queue: str,
        payload: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
    ) -> str:
        import aio_pika

        await self._declare_queue(queue)
        channel = await self._ensure_channel()
        msg_id = uuid.uuid4().hex
        merged_headers: dict[str, Any] = propagation_headers()
        if headers:
            merged_headers.update(headers)
        merged_headers.setdefault("x-sentinel-attempts", 1)

        body = json.dumps(payload, default=str).encode("utf-8")
        amqp_msg = aio_pika.Message(
            body=body,
            message_id=msg_id,
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            headers=merged_headers,
        )
        await channel.default_exchange.publish(amqp_msg, routing_key=queue)
        QUEUE_MESSAGES_PUBLISHED.labels(project="sentinel", service=self.service, queue=queue).inc()
        return msg_id

    async def consume(
        self,
        queue: str,
        handler: MessageHandler,
        *,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        stop = stop_event or asyncio.Event()
        q = await self._declare_queue(queue)
        async with q.iterator(timeout=1.0) as queue_iter:
            while not stop.is_set():
                try:
                    raw_msg = await anext(queue_iter)
                except (TimeoutError, StopAsyncIteration):
                    continue
                raw_headers = dict(raw_msg.headers or {})
                attempts = int(raw_headers.get("x-sentinel-attempts", 1))
                payload = json.loads(raw_msg.body.decode("utf-8"))
                msg = QueueMessage(
                    message_id=raw_msg.message_id or uuid.uuid4().hex,
                    queue=queue,
                    payload=payload,
                    headers={str(k): str(v) for k, v in raw_headers.items()},
                    attempts=attempts,
                    _raw=raw_msg,
                )
                extract_queue_context(msg.headers)
                try:
                    await handler(msg)
                except Exception:
                    await self.nack(msg, requeue=True)

    async def ack(self, message: QueueMessage) -> None:
        if message._raw is not None:
            await message._raw.ack()
        QUEUE_MESSAGES_CONSUMED.labels(
            project="sentinel", service=self.service, queue=message.queue, status="acked"
        ).inc()

    async def nack(
        self,
        message: QueueMessage,
        *,
        requeue: bool = True,
        count_attempt: bool = True,
    ) -> None:
        import aio_pika

        channel = await self._ensure_channel()
        if requeue and (not count_attempt or message.attempts < self.max_deliveries):
            # Republish with attempt header (unchanged on cross-executor handoff, incremented on retry)
            next_headers: dict[str, Any] = dict(message.headers)
            next_headers["x-sentinel-attempts"] = (
                message.attempts + 1 if count_attempt else message.attempts
            )
            body = json.dumps(message.payload, default=str).encode("utf-8")
            await channel.default_exchange.publish(
                aio_pika.Message(
                    body=body,
                    message_id=message.message_id,
                    content_type="application/json",
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                    headers=next_headers,
                ),
                routing_key=message.queue,
            )
            if message._raw is not None:
                await message._raw.ack()
            if count_attempt:
                QUEUE_MESSAGES_CONSUMED.labels(
                    project="sentinel", service=self.service, queue=message.queue, status="retried"
                ).inc()
        else:
            # Reject without requeue -> RabbitMQ routes directly to `<queue>.dlq`
            if message._raw is not None:
                await message._raw.reject(requeue=False)
            QUEUE_MESSAGES_CONSUMED.labels(
                project="sentinel",
                service=self.service,
                queue=message.queue,
                status="dead_lettered",
            ).inc()

    async def dead_letter_count(self, queue: str) -> int:
        channel = await self._ensure_channel()
        dlq = await channel.declare_queue(f"{queue}.dlq", durable=True, passive=False)
        count = int(dlq.declaration_result.message_count or 0)
        QUEUE_DEAD_LETTERS.labels(project="sentinel", queue=queue).set(count)
        return count

    async def backlog_count(self, queue: str) -> int:
        q = await self._declare_queue(queue)
        count = int(q.declaration_result.message_count or 0)
        QUEUE_BACKLOG.labels(project="sentinel", queue=queue).set(count)
        return count

    async def close(self) -> None:
        if self._conn is not None and not self._conn.is_closed:
            await self._conn.close()


def create_queue_client(url: str, *, service: str = "sentinel") -> QueueClient:
    if not url or url.startswith("memory://"):
        return InMemoryQueue(service=service)
    return RabbitMQQueue(url, service=service)
