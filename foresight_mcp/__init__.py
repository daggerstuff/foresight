"""
Foresight MCP Server - Full memory system with psychological safety features.
Restored from src/lib/ai/memory/ architecture.

Includes:
- MemoryObject with emotional context and empathy metrics
- Socratic Gate for psychological safety
- Anomaly Detection (mental health, extensible to other domains)
- Memory Synthesizer for reconciliation and stance shift detection
- Memory Linker for vector store and ghost nodes
- Composable memory block schemas with BlockRegistry
- Subconscious memory blocks (guidance, pending_items, preferences, patterns)
- Event bus with persistence and audit trail
- Event hook system for extensibility (HTTP webhooks, callables, async)
- Memory versioning with rollback capabilities
- Multi-tenant isolation with rate limiting
- Compliance exporters for HIPAA, SOC2, GDPR
"""
# Block registry exports
from .block_registry import (
    BlockRegistry,
    BlockScope,
    InjectionPoint,
    MemoryBlock,
    MemoryBlockSchema,
    MergeStrategy,
    RetentionPolicy,
    get_registry,
    initialize_default_blocks,
)

# Enhanced synthesizer exports
from .enhanced_synthesizer import (
    Contradiction,
    EnhancedMemorySynthesizer,
    EnhancedSynthesisResult,
    Insight,
    TemporalTrend,
    get_enhanced_synthesizer,
    reset_enhanced_synthesizer,
)

# Entity and graph exports
from .entity_extractor import (
    Entity,
    EntityExtractor,
    ExtractionResult,
    Relationship,
    get_entity_extractor,
    reset_entity_extractor,
)

# Event bus exports
from .event_bus import (
    Event,
    EventBus,
    EventType,
    get_event_bus,
)
from .graph_store import (
    GraphStore,
    GraphTraversalResult,
    get_graph_store,
    reset_graph_store,
)

# Hook system exports
from .hooks import (
    HookExecutor,
    HookRegistration,
    HookRegistry,
    HookType,
    get_hook_executor,
    list_hooks,
    register_hook,
    unregister_hook,
)

# Hybrid retriever exports
from .hybrid_retriever import (
    HybridResult,
    HybridRetriever,
    HybridSearchResult,
    get_hybrid_retriever,
    reset_hybrid_retriever,
)

# Reflection engine exports
from .reflection_engine import (
    ReflectionEngine,
    ReflectionInsight,
    ReflectionReport,
    get_reflection_engine,
    reset_reflection_engine,
)
from .server import (
    add_subconscious_guidance,
    archive_memory,
    # Audit tools
    audit_build,
    audit_export,
    audit_list_reports,
    audit_summary,
    clear_subconscious_block,
    compliance_gdpr_data_export,
    compliance_gdpr_erasure_certification,
    # Compliance exporters
    compliance_hipaa_access_log,
    compliance_hipaa_modification_log,
    compliance_hipaa_user_activity,
    compliance_save_report,
    compliance_soc2_access_review,
    compliance_soc2_change_history,
    compliance_soc2_monitoring,
    # Multi-tenant isolation
    create_tenant,
    delete_memory,
    diff_memories,
    get_memory,
    # Versioning tools
    get_memory_versions,
    get_subconscious_block,
    # Subconscious tools
    get_subconscious_blocks,
    get_subconscious_context,
    get_subconscious_whisper,
    get_tenant,
    get_tenant_isolation_status,
    list_memories,
    list_tenants,
    mcp,
    memory_status,
    process_session_transcript,
    query_memories,
    reset_subconscious_block,
    rollback_memory,
    store_memory,
    switch_tenant,
    synthesize_memories,
    update_memory,
    update_subconscious_block,
    update_tenant_config,
)
from .temporal_queries import (
    TemporalQueryBuilder,
    TemporalQueryResult,
    TimeWindow,
    get_temporal_query_builder,
    reset_temporal_query_builder,
)
from .temporal_schema import (
    initialize_decay_config,
    run_temporal_migrations,
)

# Temporal memory exports
from .temporal_service import (
    DecayConfig,
    FreshnessTrend,
    TemporalService,
    get_temporal_service,
    reset_temporal_service,
)

# Stream producer exports (optional - may not have kafka-python installed)
try:
    from .stream_producer import (
        KafkaProducer,
        KinesisProducer,
        MockProducer,
        StreamEvent,
        StreamProducer,
        StreamPublisher,
        StreamType,
        create_stream_producer,
    )
    _stream_producer_available = True
except ImportError:
    _stream_producer_available = False

    class _OptionalStreamDependencyStub:
        """Stub for optional stream dependency that raises ImportError on any use."""

        def __init__(self, *args, **kwargs):
            raise ImportError(
                f"{self.__class__.__name__} requires kafka-python or boto3. "
                f"Install with: pip install kafka-python boto3"
            )

    class StreamProducer(_OptionalStreamDependencyStub): ...
    class StreamPublisher(_OptionalStreamDependencyStub): ...
    class StreamEvent(_OptionalStreamDependencyStub): ...
    class StreamType(_OptionalStreamDependencyStub): ...
    class KafkaProducer(_OptionalStreamDependencyStub): ...
    class KinesisProducer(_OptionalStreamDependencyStub): ...
    class MockProducer(_OptionalStreamDependencyStub): ...

    def create_stream_producer(*args, **kwargs):
        raise ImportError(
            "create_stream_producer requires kafka-python or boto3. "
            "Install with: pip install kafka-python boto3"
        )

# Consumer group exports (optional - may not have kafka-python installed)
try:
    from .consumer_group import (
        ConsumerRecord,
        ConsumerState,
        ConsumerStats,
        KafkaConsumerGroup,
    )
    _consumer_group_available = True
except ImportError:
    _consumer_group_available = False

    class _OptionalConsumerDependencyStub:
        """Stub for optional consumer dependency that raises ImportError on any use."""

        def __init__(self, *args, **kwargs):
            raise ImportError(
                f"{self.__class__.__name__} requires kafka-python. "
                f"Install with: pip install kafka-python"
            )

    class KafkaConsumerGroup(_OptionalConsumerDependencyStub): ...
    class ConsumerRecord(_OptionalConsumerDependencyStub): ...
    class ConsumerStats(_OptionalConsumerDependencyStub): ...
    class ConsumerState(_OptionalConsumerDependencyStub): ...
# Hook system exports
# CRDT exports
from .crdt import (
    LWWMap,
    LWWRegister,
    ORSet,
    VectorClock,
)

# Projections exports
from .projections.builder import ProjectionBuilder
from .projections.reports import (
    AccessLog,
    AnomalyReport,
    BlockChangeLog,
    MemoryTimeline,
    UserActivityReport,
)

# Sync exports
from .sync import (
    Operation,
    OperationQueue,
    OperationType,
    SyncManager,
    SyncProgress,
    SyncStatus,
    get_sync_manager,
    reset_sync_manager,
)
from .websocket.server import (
    Connection,
    ConnectionState,
    WebSocketHandler,
    WebSocketServer,
)
from .websocket.subscriptions import (
    Subscription,
    SubscriptionManager,
    get_subscription_manager,
    reset_subscription_manager,
)

__version__ = "1.2.0"
__all__ = [
    "mcp",
    "store_memory",
    "query_memories",
    "list_memories",
    "get_memory",
    "update_memory",
    "delete_memory",
    "memory_status",
    "synthesize_memories",
    "archive_memory",
    # Versioning tools
    "get_memory_versions",
    "rollback_memory",
    "diff_memories",
    # Multi-tenant isolation
    "create_tenant",
    "get_tenant",
    "list_tenants",
    "update_tenant_config",
    "switch_tenant",
    "get_tenant_isolation_status",
    # Compliance exporters
    "compliance_hipaa_access_log",
    "compliance_hipaa_modification_log",
    "compliance_hipaa_user_activity",
    "compliance_soc2_change_history",
    "compliance_soc2_access_review",
    "compliance_soc2_monitoring",
    "compliance_gdpr_data_export",
    "compliance_gdpr_erasure_certification",
    "compliance_save_report",
    # Subconscious
    "get_subconscious_blocks",
    "get_subconscious_block",
    "update_subconscious_block",
    "add_subconscious_guidance",
    "get_subconscious_whisper",
    "get_subconscious_context",
    "reset_subconscious_block",
    "clear_subconscious_block",
    "process_session_transcript",
    # Audit tools
    "audit_build",
    "audit_list_reports",
    "audit_export",
    "audit_summary",
    # Block registry
    "BlockRegistry",
    "MemoryBlockSchema",
    "MemoryBlock",
    "RetentionPolicy",
    "MergeStrategy",
    "InjectionPoint",
    "BlockScope",
    "get_registry",
    "initialize_default_blocks",
    # Stream processing
    "StreamProducer",
    "StreamPublisher",
    "StreamEvent",
    "StreamType",
    "KafkaProducer",
    "KinesisProducer",
    "MockProducer",
    "create_stream_producer",
    # Consumer group
    "KafkaConsumerGroup",
    "ConsumerRecord",
    "ConsumerStats",
    "ConsumerState",
    # WebSocket
    "WebSocketServer",
    "WebSocketHandler",
    "ConnectionState",
    "Connection",
    "SubscriptionManager",
    "Subscription",
    "get_subscription_manager",
    "reset_subscription_manager",
    # CRDT
    "VectorClock",
    "LWWRegister",
    "ORSet",
    "LWWMap",
    # Sync
    "SyncManager",
    "SyncStatus",
    "OperationType",
    "Operation",
    "OperationQueue",
    "SyncProgress",
    "get_sync_manager",
    "reset_sync_manager",
    # Hybrid retriever
    "HybridRetriever",
    "HybridResult",
    "HybridSearchResult",
    "get_hybrid_retriever",
    "reset_hybrid_retriever",
    # Reflection engine
    "ReflectionEngine",
    "ReflectionReport",
    "ReflectionInsight",
    "get_reflection_engine",
    "reset_reflection_engine",
]
