"""Tests for the BlockRegistry singleton and MemoryBlockSchema lifecycle."""
import pytest
from foresight_mcp.block_registry import (
    BlockRegistry,
    MemoryBlockSchema,
    MemoryBlock,
    RetentionPolicy,
    MergeStrategy,
    InjectionPoint,
    BlockScope,
    get_registry,
    initialize_default_blocks,
    DEFAULT_BLOCK_SCHEMAS,
)


@pytest.fixture
def registry():
    """Fixture providing a clean registry for each test."""
    # Reset singleton instance
    BlockRegistry._instance = None
    reg = BlockRegistry()
    yield reg
    # Cleanup after test
    BlockRegistry._instance = None


@pytest.fixture
def test_schema():
    """A standard test schema."""
    return MemoryBlockSchema(
        label="test_block",
        description="A test block schema",
        content="Default content",
        retention_policy=RetentionPolicy.SHORT_TERM,
        merge_strategy=MergeStrategy.APPEND,
        injection_point=InjectionPoint.PRE_PROMPT,
        scope=BlockScope.SESSION,
    )


def test_singleton_pattern():
    """Test that BlockRegistry is a singleton."""
    BlockRegistry._instance = None
    reg1 = BlockRegistry()
    reg2 = BlockRegistry()

    assert reg1 is reg2

    reg3 = get_registry()
    assert reg1 is reg3


def test_register_schema(registry, test_schema):
    """Test registering a new schema."""
    registry.register(test_schema)

    assert registry.get_schema("test_block") is test_schema
    assert test_schema in registry.list_schemas()


def test_register_duplicate_schema(registry, test_schema):
    """Test registering a duplicate schema raises ValueError."""
    registry.register(test_schema)

    with pytest.raises(ValueError, match="Block schema 'test_block' already registered"):
        registry.register(test_schema)


def test_get_nonexistent_schema(registry):
    """Test getting a schema that doesn't exist."""
    assert registry.get_schema("nonexistent") is None


def test_create_block(registry, test_schema):
    """Test creating a block from a registered schema."""
    registry.register(test_schema)

    block = registry.create_block("test_block", content="Custom content")

    assert isinstance(block, MemoryBlock)
    assert block.schema is test_schema
    assert block.content == "Custom content"


def test_create_block_default_content(registry, test_schema):
    """Test creating a block uses empty string if content not provided."""
    registry.register(test_schema)

    block = registry.create_block("test_block")

    assert block.content == ""


def test_create_block_unregistered_schema(registry):
    """Test creating a block for an unregistered schema raises ValueError."""
    with pytest.raises(ValueError, match="Block schema 'unregistered' not found"):
        registry.create_block("unregistered", content="foo")


def test_block_lifecycle(registry, test_schema):
    """Test block setting, getting, and deletion."""
    registry.register(test_schema)

    block = registry.create_block("test_block", content="Test")

    # Block is not in registry until set
    assert registry.get_block("test_block") is None

    registry.set_block("test_block", block)

    assert registry.get_block("test_block") is block
    assert block in registry.list_blocks()

    # Delete block
    assert registry.delete_block("test_block") is True
    assert registry.get_block("test_block") is None

    # Delete non-existent block
    assert registry.delete_block("test_block") is False


def test_clear_blocks(registry, test_schema):
    """Test clearing all blocks."""
    registry.register(test_schema)

    block1 = registry.create_block("test_block", content="One")
    registry.set_block("test_block_1", block1)

    block2 = registry.create_block("test_block", content="Two")
    registry.set_block("test_block_2", block2)

    assert len(registry.list_blocks()) == 2

    registry.clear()

    assert len(registry.list_blocks()) == 0
    # Schema should still be there
    assert registry.get_schema("test_block") is not None


def test_initialize_default_blocks():
    """Test initialization of default blocks."""
    # Ensure a clean slate
    BlockRegistry._instance = None

    registry = initialize_default_blocks()

    schemas = registry.list_schemas()

    assert len(schemas) == len(DEFAULT_BLOCK_SCHEMAS)

    for default_schema in DEFAULT_BLOCK_SCHEMAS:
        registered_schema = registry.get_schema(default_schema.label)
        assert registered_schema is not None
        assert registered_schema.label == default_schema.label
        assert registered_schema.description == default_schema.description

    # Calling it again shouldn't fail due to duplicate registrations
    # (contextlib.suppress(ValueError) should handle it)
    registry2 = initialize_default_blocks()
    assert registry is registry2
    assert len(registry.list_schemas()) == len(DEFAULT_BLOCK_SCHEMAS)

def test_schema_validate_content_char_limit(test_schema):
    """Test validation fails when char limit is exceeded."""
    test_schema.char_limit = 10
    is_valid, msg = test_schema.validate_content("This is longer than 10 characters")
    assert is_valid is False
    assert "Content exceeds char limit" in msg


def test_schema_validate_content_custom_validator(test_schema):
    """Test custom validator logic."""
    test_schema.validator = lambda c: "valid" in c
    is_valid, msg = test_schema.validate_content("This is bad content")
    assert is_valid is False
    assert msg == "Custom validation failed"

    is_valid, msg = test_schema.validate_content("This is valid content")
    assert is_valid is True
    assert msg == ""


def test_schema_validate_content_valid(test_schema):
    """Test validation passes for valid content."""
    is_valid, msg = test_schema.validate_content("Some valid content")
    assert is_valid is True
    assert msg == ""


def test_schema_to_dict(test_schema):
    """Test serialization of schema."""
    d = test_schema.to_dict()
    assert d["label"] == "test_block"
    assert d["description"] == "A test block schema"
    assert d["content"] == "Default content"
    assert d["retention_policy"] == "short_term"
    assert d["merge_strategy"] == "append"
    assert d["injection_point"] == "pre_prompt"
    assert d["scope"] == "session"
    assert d["char_limit"] == 0
    assert d["metadata"] == {}


def test_schema_from_dict():
    """Test deserialization of schema."""
    data = {
        "label": "deserialized",
        "description": "Deserialized schema",
        "content": "Initial",
        "retention_policy": "long_term",
        "merge_strategy": "replace",
        "injection_point": "whisper_only",
        "scope": "project",
        "char_limit": 100,
        "metadata": {"key": "value"}
    }
    schema = MemoryBlockSchema.from_dict(data)

    assert schema.label == "deserialized"
    assert schema.description == "Deserialized schema"
    assert schema.content == "Initial"
    assert schema.retention_policy == RetentionPolicy.LONG_TERM
    assert schema.merge_strategy == MergeStrategy.REPLACE
    assert schema.injection_point == InjectionPoint.WHISPER_ONLY
    assert schema.scope == BlockScope.PROJECT
    assert schema.char_limit == 100
    assert schema.metadata == {"key": "value"}


def test_block_update_content_replace(registry, test_schema):
    """Test update_content with REPLACE strategy."""
    test_schema.merge_strategy = MergeStrategy.REPLACE
    registry.register(test_schema)
    block = registry.create_block("test_block", "Old content")

    initial_version = block.version

    block.update_content("New content")

    assert block.content == "New content"
    assert block.version == initial_version + 1


def test_block_update_content_append(registry, test_schema):
    """Test update_content with APPEND strategy."""
    test_schema.merge_strategy = MergeStrategy.APPEND
    registry.register(test_schema)

    # Appending to empty block
    block_empty = registry.create_block("test_block", "")
    block_empty.update_content("New content")
    assert block_empty.content == "New content"

    # Appending to non-empty block
    block_non_empty = registry.create_block("test_block", "Old content")
    block_non_empty.update_content("New content")
    assert block_non_empty.content == "Old content\nNew content"


def test_block_update_content_synthesize(registry, test_schema):
    """Test update_content with SYNTHESIZE strategy falls back to replace."""
    test_schema.merge_strategy = MergeStrategy.SYNTHESIZE
    registry.register(test_schema)
    block = registry.create_block("test_block", "Old content")

    block.update_content("New content")

    assert block.content == "New content"


def test_block_is_empty(registry, test_schema):
    """Test is_empty logic."""
    registry.register(test_schema)

    block_empty = registry.create_block("test_block", "")
    assert block_empty.is_empty() is True

    block_spaces = registry.create_block("test_block", "   \n  ")
    assert block_spaces.is_empty() is True

    block_default = registry.create_block("test_block", "(No guidance provided yet)")
    assert block_default.is_empty() is True

    block_valid = registry.create_block("test_block", "Actual content")
    assert block_valid.is_empty() is False


def test_block_to_dict(registry, test_schema):
    """Test serialization of a block."""
    registry.register(test_schema)
    block = registry.create_block("test_block", "Content")

    d = block.to_dict()
    assert d["content"] == "Content"
    assert "schema" in d
    assert d["schema"]["label"] == "test_block"
    assert "created_at" in d
    assert "updated_at" in d
    assert d["version"] == 0
