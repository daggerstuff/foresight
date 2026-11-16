"""Tests for CRDT implementations."""

import time

import pytest

from foresight.crdt import (
    LWWMap,
    LWWRegister,
    ORSet,
    VectorClock,
)


class TestVectorClock:
    """Test Vector Clock implementation."""

    def test_increment(self):
        """Test incrementing clock."""
        clock = VectorClock()
        increments = 0
        clock.increment("node-1")
        increments += 1
        assert clock.clock["node-1"] == increments

        clock.increment("node-1")
        increments += 1
        assert clock.clock["node-1"] == increments

    def test_merge(self):
        """Test merging two clocks."""
        left = {"a": 1, "b": 2}
        right = {"b": 3, "c": 1}

        clock1 = VectorClock()
        clock1.clock = dict(left)

        clock2 = VectorClock()
        clock2.clock = dict(right)

        clock1.merge(clock2)
        assert clock1.clock["a"] == left["a"]
        assert clock1.clock["b"] == max(left["b"], right["b"])
        assert clock1.clock["c"] == right["c"]

    def test_happens_before(self):
        """Test happens-before relationship."""
        clock1 = VectorClock()
        clock1.clock = {"a": 1, "b": 1}

        clock2 = VectorClock()
        clock2.clock = {"a": 2, "b": 2}

        assert clock1.happens_before(clock2)
        assert not clock2.happens_before(clock1)

    def test_concurrent(self):
        """Test concurrent clocks."""
        clock1 = VectorClock()
        clock1.clock = {"a": 2, "b": 1}

        clock2 = VectorClock()
        clock2.clock = {"a": 1, "b": 2}

        assert clock1.concurrent_with(clock2)
        assert clock2.concurrent_with(clock1)

    def test_copy(self):
        """Test copying vector clock."""
        clock1 = VectorClock()
        clock1.clock = {"a": 1, "b": 2}

        copied_value = 999
        clock2 = clock1.copy()
        clock2.clock["a"] = copied_value

        assert clock1.clock["a"] == 1
        assert clock2.clock["a"] == copied_value


class TestLWWRegister:
    """Test Last-Writer-Wins Register."""

    def test_set_and_get(self):
        """Test basic set and get."""
        register = LWWRegister()
        register.set("value1", "node-1", timestamp=1.0)
        assert register.get() == "value1"
        assert register.node_id == "node-1"

    def test_last_writer_wins(self):
        """Test that last write wins."""
        register = LWWRegister()

        # First write
        register.set("value1", "node-1", timestamp=1.0)
        assert register.get() == "value1"

        # Later write wins
        register.set("value2", "node-2", timestamp=2.0)
        assert register.get() == "value2"

        # Earlier write doesn't change
        register.set("value3", "node-3", timestamp=1.5)
        assert register.get() == "value2"

    def test_tie_breaker(self):
        """Test tie-breaking by node_id."""
        register = LWWRegister()

        # Same timestamp, different nodes
        register.set("value1", "node-a", timestamp=1.0)
        assert register.get() == "value1"

        # Same timestamp, lexicographically higher node wins
        register.set("value2", "node-b", timestamp=1.0)
        assert register.get() == "value2"

    def test_merge(self):
        """Test merging two registers."""
        register1 = LWWRegister()
        register1.set("value1", "node-1", timestamp=1.0)

        register2 = LWWRegister()
        register2.set("value2", "node-2", timestamp=2.0)

        register1.merge(register2)
        assert register1.get() == "value2"

    def test_to_from_dict(self):
        """Test serialization."""
        timestamp = 123.0
        register1 = LWWRegister()
        register1.set("test", "node-1", timestamp=timestamp)

        data = register1.to_dict()
        register2 = LWWRegister.from_dict(data)

        assert register2.get() == "test"
        assert register2.timestamp == timestamp


class TestORSet:
    """Test Observed-Remove Set."""

    def test_add_and_contains(self):
        """Test adding elements."""
        orset = ORSet()
        orset.add("element1")
        orset.add("element2")

        assert orset.contains("element1") is True
        assert orset.contains("element2") is True
        assert orset.get_elements() == {"element1", "element2"}

    def test_remove(self):
        """Test removing elements."""
        orset = ORSet()
        orset.add("element1")
        orset.remove("element1")

        # After remove, the add tags should be in removes
        element_hash = orset._get_hash("element1")
        assert element_hash in orset._removes

    def test_merge(self):
        """Test merging two OR-Sets."""
        orset1 = ORSet()
        orset1.add("a")
        orset1.add("b")

        orset2 = ORSet()
        orset2.add("c")
        orset2.add("d")

        orset1.merge(orset2)

        assert orset1.get_elements() == {"a", "b", "c", "d"}

    def test_to_from_dict(self):
        """Test serialization."""
        orset1 = ORSet()
        orset1.add("test")

        data = orset1.to_dict()
        orset2 = ORSet.from_dict(data)

        assert orset2.get_elements() == {"test"}


class TestLWWMap:
    """Test Last-Writer-Wins Map."""

    def test_set_and_get(self):
        """Test basic set and get."""
        lww_map = LWWMap()
        lww_map.set("key1", "value1")
        assert lww_map.get("key1") == "value1"

    def test_update(self):
        """Test updating existing key."""
        lww_map = LWWMap()
        lww_map.set("key1", "value1")
        time.sleep(0.01)  # Ensure different timestamps
        lww_map.set("key1", "value2")
        assert lww_map.get("key1") == "value2"

    def test_delete(self):
        """Test deleting a key."""
        lww_map = LWWMap()
        lww_map.set("key1", "value1")
        lww_map.delete("key1")
        assert lww_map.get("key1") is None

    def test_merge(self):
        """Test merging two maps."""
        first_entries = {"a": 1, "b": 2}
        second_entries = {"c": 3, "d": 4}

        map1 = LWWMap()
        for key, value in first_entries.items():
            map1.set(key, value)

        map2 = LWWMap()
        for key, value in second_entries.items():
            map2.set(key, value)

        map1.merge(map2)

        for key, value in {**first_entries, **second_entries}.items():
            assert map1.get(key) == value

    def test_merge_conflict(self):
        """Test merge with conflicting updates."""
        map1 = LWWMap()
        map1.set(
            "key",
            "value1",
        )

        time.sleep(0.01)

        map2 = LWWMap()
        map2.set("key", "value2")

        # map2 has later timestamp, should win
        map1.merge(map2)
        assert map1.get("key") == "value2"

    def test_to_from_dict(self):
        """Test serialization."""
        map1 = LWWMap()
        map1.set("key1", "value1")
        map1.set("key2", "value2")

        data = map1.to_dict()
        map2 = LWWMap.from_dict(data)

        assert map2.get("key1") == "value1"
        assert map2.get("key2") == "value2"


class TestSplitBrain:
    """Test split-brain scenarios."""

    def test_network_partition(self):
        """Test network partition recovery."""
        # Node A and B both update same key during partition
        register_a = LWWRegister()
        register_b = LWWRegister()

        # Both start with same value at t=100
        register_a.set("initial", "system", timestamp=100.0)
        register_b.set("initial", "system", timestamp=100.0)

        # Partition: both nodes update independently
        # node-b has later timestamp, so it wins
        register_a.set("value-a", "node-a", timestamp=101.0)
        register_b.set("value-b", "node-b", timestamp=102.0)

        # Heal partition: merge
        register_a.merge(register_b)

        # After merge, should converge to latest timestamp
        assert register_a.get() == "value-b"

    def test_concurrent_adds_to_set(self):
        """Test concurrent adds to OR-Set."""
        orset1 = ORSet()
        orset2 = ORSet()

        # Concurrent adds
        added_elements = ("a", "b")
        orset1.add(added_elements[0])
        orset2.add(added_elements[1])

        # Merge
        orset1.merge(orset2)
        orset2.merge(orset1)

        # Both should have both elements
        assert len(orset1._adds) == len(added_elements)
        assert len(orset2._adds) == len(added_elements)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
