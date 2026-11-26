from __future__ import annotations

from foresight.hybrid_retriever import HybridRetriever

USER_A = "user-a"
USER_B = "user-b"
TENANT = "tenant-a"
OTHER_TENANT = "tenant-b"


def _cache_key(user_id: str, tenant_id: str) -> tuple:
    return ("deploy status", user_id, tenant_id, False, (), (), None, 5, 0.0, False)


def test_invalidating_one_scope_keeps_another_users_cached_results() -> None:
    retriever = HybridRetriever(db_path=":memory:")
    mine = _cache_key(USER_A, TENANT)
    theirs = _cache_key(USER_B, TENANT)
    other_tenant = _cache_key(USER_A, OTHER_TENANT)
    for key in (mine, theirs, other_tenant):
        retriever._cache_result(key, "cached")

    retriever.invalidate_caches(USER_A, TENANT)

    assert retriever._get_cached_result(mine) is None
    assert retriever._get_cached_result(theirs) is not None
    assert retriever._get_cached_result(other_tenant) is not None

    retriever.invalidate_caches()

    assert retriever._get_cached_result(theirs) is None
    assert retriever._get_cached_result(other_tenant) is None
