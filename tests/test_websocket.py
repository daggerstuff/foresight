"""Tests for WebSocket server and subscriptions."""

import asyncio
import contextlib
from datetime import UTC
from unittest.mock import patch

import pytest

from foresight.event_bus import EventType
from foresight.websocket.server import (
    DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    DEFAULT_HEARTBEAT_TIMEOUT_SECONDS,
    ConnectionState,
    WebSocketHandler,
    WebSocketServer,
)
from foresight.websocket.subscriptions import (
    get_subscription_manager,
    reset_subscription_manager,
)


@pytest.fixture
def event_loop():
    """Create event loop for tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(autouse=True)
def reset_subscriptions():
    """Reset subscription manager before each test."""
    reset_subscription_manager()


class TestWebSocketHandler:
    """Test WebSocket handler."""

    @pytest.mark.asyncio
    async def test_connect_new_connection(self):
        """Test connecting a new client."""
        handler = WebSocketHandler()
        conn_id, response = await handler.connect(tenant_id="tenant-1", user_id="user-1")

        assert conn_id is not None
        assert response["type"] == "connection_accepted"
        assert response["connection_id"] == conn_id
        assert response["heartbeat_interval"] == int(DEFAULT_HEARTBEAT_INTERVAL_SECONDS)

    @pytest.mark.asyncio
    async def test_reconnect_connection(self):
        """Test reconnecting with existing connection ID."""
        handler = WebSocketHandler()

        # First connection
        conn_id, _ = await handler.connect(connection_id="conn-123")
        assert conn_id == "conn-123"

        # Reconnection
        _, response = await handler.connect(connection_id="conn-123")
        assert response["type"] == "reconnected"
        assert response["connection_id"] == "conn-123"

    @pytest.mark.asyncio
    async def test_disconnect_connection(self):
        """Test disconnecting a connection."""
        handler = WebSocketHandler()

        conn_id, _ = await handler.connect()
        await handler.disconnect(conn_id)

        conn = handler.get_connection(conn_id)
        assert conn is not None
        assert conn.state == ConnectionState.DISCONNECTED


class TestSubscriptionManager:
    """Test subscription manager."""

    @pytest.mark.asyncio
    async def test_subscribe(self):
        """Test creating a subscription."""
        sub_manager = get_subscription_manager()

        event_types = ["memory.stored", "memory.updated"]
        result = await sub_manager.subscribe(
            subscription_id="sub-1",
            connection_id="conn-1",
            event_types=event_types,
            entity_filter="memory:*",
        )

        assert result.id == "sub-1"
        assert result.connection_id == "conn-1"
        assert len(result.event_types) == len(event_types)

    @pytest.mark.asyncio
    async def test_unsubscribe(self):
        """Test removing a subscription."""
        sub_manager = get_subscription_manager()

        await sub_manager.subscribe(
            subscription_id="sub-1",
            connection_id="conn-1",
            event_types=["memory.stored"],
        )

        result = await sub_manager.unsubscribe("sub-1")
        assert result is True

    @pytest.mark.asyncio
    async def test_subscription_matches_event(self):
        """Test subscription event matching."""
        sub_manager = get_subscription_manager()

        await sub_manager.subscribe(
            subscription_id="sub-1",
            connection_id="conn-1",
            event_types=["memory.stored", "memory.updated"],
        )

        # Should match
        subs = sub_manager.get_matching_subscriptions(EventType.MEMORY_STORED)
        assert len(subs) == 1

        # Should not match
        subs = sub_manager.get_matching_subscriptions(EventType.MEMORY_DELETED)
        assert len(subs) == 0


class TestWebSocketServer:
    """Test WebSocket server integration."""

    @pytest.mark.asyncio
    async def test_server_initialization(self):
        """Test server initializes correctly."""
        server = WebSocketServer()
        assert server.handler is not None
        assert server._heartbeat_interval == DEFAULT_HEARTBEAT_INTERVAL_SECONDS
        assert server._heartbeat_timeout == DEFAULT_HEARTBEAT_TIMEOUT_SECONDS

    @pytest.mark.asyncio
    async def test_server_with_auth_callback(self):
        """Test server with authentication callback."""

        def auth_func(token):
            return token == "valid-token"

        server = WebSocketServer(auth_callback=auth_func)
        assert server.handler._auth_callback == auth_func


class TestConnectionState:
    """Test connection state management."""

    @pytest.mark.asyncio
    async def test_connection_state_transitions(self):
        """Test connection state transitions."""
        handler = WebSocketHandler()

        # Connect
        conn_id, _ = await handler.connect()
        conn = handler.get_connection(conn_id)
        assert conn.state == ConnectionState.CONNECTED

        # Disconnect
        await handler.disconnect(conn_id)
        assert conn.state == ConnectionState.DISCONNECTED


class TestEventBuffer:
    """Test event buffer for reconnection replay."""

    @pytest.mark.asyncio
    async def test_push_event(self):
        handler = WebSocketHandler()
        payload = {"id": "evt-1", "event_type": "memory.stored"}
        handler.push_event(payload)
        assert len(handler._event_buffer) == 1
        assert handler._event_buffer[0]["id"] == "evt-1"

    @pytest.mark.asyncio
    async def test_get_buffered_events_all(self):
        handler = WebSocketHandler()
        event_count = 5
        for i in range(event_count):
            handler.push_event({"id": f"evt-{i}"})
        result = handler.get_buffered_events()
        assert len(result) == event_count

    @pytest.mark.asyncio
    async def test_get_buffered_events_since_id(self):
        handler = WebSocketHandler()
        event_ids = ("evt-0", "evt-1", "evt-2")
        for event_id in event_ids:
            handler.push_event({"id": event_id})
        result = handler.get_buffered_events(since_event_id=event_ids[0])
        assert len(result) == len(event_ids)
        assert result[0]["id"] == "evt-0"

    @pytest.mark.asyncio
    async def test_get_buffered_events_unknown_id_returns_all(self):
        handler = WebSocketHandler()
        event_ids = ("evt-0", "evt-1")
        for event_id in event_ids:
            handler.push_event({"id": event_id})
        result = handler.get_buffered_events(since_event_id="unknown")
        assert len(result) == len(event_ids)

    @pytest.mark.asyncio
    async def test_buffer_respects_max_size(self):
        handler = WebSocketHandler()
        handler._max_buffer_size = 3
        for i in range(5):
            handler.push_event({"id": f"evt-{i}"})
        assert len(handler._event_buffer) == handler._max_buffer_size
        assert handler._event_buffer[0]["id"] == "evt-2"

    @pytest.mark.asyncio
    async def test_reconnect_replays_buffered_events(self):
        handler = WebSocketHandler()
        conn_id, _ = await handler.connect(connection_id="replay-conn")
        conn = handler.get_connection(conn_id)
        replayable_ids = ("evt-0", "evt-1", "evt-2")
        for event_id in replayable_ids:
            handler.push_event({"id": event_id})

        with patch.object(conn, "send_message") as mock_send:
            _, response = await handler.connect(connection_id="replay-conn")
            assert response["type"] == "reconnected"
            replay_count = sum(
                1 for call_args in mock_send.call_args_list if call_args[0][0].get("type") == "event_replay"
            )
            assert replay_count == len(replayable_ids)


class TestBroadcastEvent:
    """Test subscription-aware event routing."""

    @pytest.mark.asyncio
    async def test_broadcast_to_subscribed_connection(self):
        handler = WebSocketHandler()
        server = WebSocketServer()
        server.handler = handler

        _, _ = await handler.connect(connection_id="conn-a")
        _, _ = await handler.connect(connection_id="conn-b")

        sub_manager = get_subscription_manager()
        await sub_manager.subscribe(
            subscription_id="sub-a",
            connection_id="conn-a",
            event_types=["memory.stored"],
        )

        sent_to_a: list = []
        sent_to_b: list = []

        def _capture_a(message):
            sent_to_a.append(message)

        def _capture_b(message):
            sent_to_b.append(message)

        handler._connections["conn-a"].send_message = _capture_a
        handler._connections["conn-b"].send_message = _capture_b

        server._broadcast_event("memory.stored", {"id": "mem-1"})

        assert len(sent_to_a) == 1
        assert len(sent_to_b) == 0

    @pytest.mark.asyncio
    async def test_broadcast_to_all_when_unparseable_type(self):
        handler = WebSocketHandler()
        server = WebSocketServer()
        server.handler = handler

        await handler.connect(connection_id="conn-a")
        await handler.connect(connection_id="conn-b")

        sent_a: list = []
        sent_b: list = []

        handler._connections["conn-a"].send_message = sent_a.append
        handler._connections["conn-b"].send_message = sent_b.append

        server._broadcast_event("unknown.type", {"id": "x"})

        assert len(sent_a) == 1
        assert len(sent_b) == 1

    @pytest.mark.asyncio
    async def test_broadcast_with_entity_id_filtering(self):
        handler = WebSocketHandler()
        server = WebSocketServer()
        server.handler = handler

        await handler.connect(connection_id="conn-a")
        await handler.connect(connection_id="conn-b")

        sub_manager = get_subscription_manager()
        await sub_manager.subscribe(
            subscription_id="sub-a",
            connection_id="conn-a",
            event_types=["memory.stored"],
            entity_filter="mem-1",
        )

        sent_a: list = []
        sent_b: list = []
        handler._connections["conn-a"].send_message = sent_a.append
        handler._connections["conn-b"].send_message = sent_b.append

        server._broadcast_event("memory.stored", {"id": "mem-1"})

        assert len(sent_a) == 1
        assert len(sent_b) == 0


class TestHeartbeat:
    """Test proactive heartbeat."""

    @pytest.mark.asyncio
    async def test_heartbeat_loop_sends_pings(self):
        handler = WebSocketHandler()
        server = WebSocketServer(heartbeat_interval=0.1, heartbeat_timeout=0.3)
        server.handler = handler

        await handler.connect(connection_id="hb-conn")
        conn = handler.get_connection("hb-conn")

        pings_received = []
        original_send = conn.send_message

        def capture(message):
            if isinstance(message, dict) and message.get("type") == "ping":
                pings_received.append(message)
            original_send(message)

        conn.send_message = capture

        server._running = True
        task = asyncio.create_task(server._heartbeat_loop())
        await asyncio.sleep(0.25)
        server._running = False
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert len(pings_received) >= 1

    @pytest.mark.asyncio
    async def test_heartbeat_disconnects_stale_connection(self):
        handler = WebSocketHandler()
        server = WebSocketServer(heartbeat_interval=0.05, heartbeat_timeout=0.1)
        server.handler = handler

        await handler.connect(connection_id="stale-conn")
        conn = handler.get_connection("stale-conn")
        from datetime import datetime, timedelta

        conn.last_heartbeat = datetime.now(UTC) - timedelta(seconds=5)

        server._running = True
        task = asyncio.create_task(server._heartbeat_loop())
        await asyncio.sleep(0.15)
        server._running = False
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert conn.state == ConnectionState.DISCONNECTED
