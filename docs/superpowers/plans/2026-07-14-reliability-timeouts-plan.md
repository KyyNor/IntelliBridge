# IntelliBridge Reliability and Timeouts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make selected IntelliBridge operations bounded, concurrency-safe, and testable without changing the Docker Compose entrypoint or FineCPT source/target direction behavior.

**Architecture:** Add a deadline context and async request-timeout middleware, use bounded resource managers for Hive and MySQL, and replace mutable cache rebuilds with atomic snapshots. Keep existing synchronous business APIs stable while moving REST blocking work into Starlette's worker threadpool. Use a bounded queue for call logs.

**Tech Stack:** Python 3, FastAPI/Starlette, FastMCP, PyHive, DBUtils/PyMySQL, `pytest` with standard-library fakes.

---

### Task 1: Add timeout primitives and bounded call-log writer

**Files:**
- Create: `utils/timeouts.py`
- Create: `utils/call_log_writer.py`
- Modify: `utils/decorators.py`
- Test: `tests/test_timeouts.py`
- Test: `tests/test_call_log_writer.py`

- [ ] Write tests for async timeout, deadline propagation, and queue-full log dropping.
- [ ] Run the focused tests and verify they fail because the new modules do not exist.
- [ ] Implement `OperationTimeout`, deadline context helpers, and `with_timeout` for async entrypoints.
- [ ] Implement a lazy-started bounded queue with one worker and explicit `stop()` for call logs.
- [ ] Update `log_function_info` to submit log records to the bounded writer without creating one thread per call.
- [ ] Run focused tests and verify they pass.
- [ ] Commit as `feat: add bounded timeout and call log primitives`.

### Task 2: Bound MySQL connection acquisition

**Files:**
- Modify: `utils/mysql_pool.py`
- Modify: `utils/config.py` only if timeout lookup needs a helper
- Test: `tests/test_mysql_pool.py`

- [ ] Write a fake-pool test proving a second acquisition waits and then raises `TimeoutError` instead of blocking indefinitely.
- [ ] Run the focused test and verify it fails.
- [ ] Add one bounded semaphore per configured node, acquire it before `PooledDB.connection()`, and release it after the connection is returned.
- [ ] Read `pool_acquire_timeout` from node configuration with a safe default.
- [ ] Preserve pool slot state when initialization fails and expose node readiness information.
- [ ] Run focused tests and the complete test suite.
- [ ] Commit as `fix: bound mysql pool acquisition`.

### Task 3: Manage Hive with two independent query leases

**Files:**
- Replace implementation in: `utils/hive_pool.py`
- Modify: `tools/hive_query.py`
- Test: `tests/test_hive_pool.py`
- Test: `tests/test_hive_query.py`

- [ ] Write tests for two simultaneous leases, a third lease timing out, and connection close/release on success and failure.
- [ ] Run focused tests and verify they fail.
- [ ] Implement `HiveQueryLeaseManager` with `max_concurrent=2`, bounded queue wait, per-query connection creation, and `close_all()`.
- [ ] Change Hive query and describe paths to use a lease context, never a shared connection/cursor.
- [ ] Keep the existing public `HiveQuery` methods and return shapes unchanged.
- [ ] Add input page validation and separate `REFRESH` from row-limited SELECT execution.
- [ ] Run focused and complete tests.
- [ ] Commit as `fix: isolate hive queries behind bounded leases`.

### Task 4: Isolate blocking REST handlers

**Files:**
- Modify: `tools/hive_query.py`
- Modify: `tools/mysql_query.py`
- Modify: `tools/agent_browser.py`
- Modify: `tools/fine_report_tools.py`
- Test: `tests/test_routes.py`

- [ ] Add route tests that patch the synchronous service method and assert the route uses the threadpool adapter.
- [ ] Run the route tests and verify they fail against the current direct synchronous calls.
- [ ] Convert blocking REST handlers to regular `def` handlers or explicitly use `run_in_threadpool`, preserving response schemas.
- [ ] Apply `with_timeout` to async request wrappers where an async handler remains.
- [ ] Run route tests and the complete test suite.
- [ ] Commit as `fix: isolate blocking rest handlers`.

### Task 5: Make cache refresh atomic

**Files:**
- Modify: `tools/ds_code_search.py`
- Modify: `tools/fine_cpt_search.py`
- Test: `tests/test_cache_snapshots.py`

- [ ] Write tests proving readers see either the old or new complete snapshot while a refresh is in progress.
- [ ] Run focused tests and verify they fail against in-place mutation.
- [ ] Build local dictionaries and indexes off to the side, then atomically swap a snapshot reference.
- [ ] Guard initial load with single-flight state and avoid holding the lock while scanning all database rows.
- [ ] Clamp page values and preserve cache statistics.
- [ ] Run focused and complete tests.
- [ ] Commit as `fix: refresh search caches atomically`.

### Task 6: Fix server-enforced SQL limits and fuzzy search

**Files:**
- Modify: `tools/mysql_query.py`
- Modify: `tools/hive_query.py`
- Modify: `tools/ds_code_search.py`
- Test: `tests/test_query_limits.py`
- Test: `tests/test_search_matching.py`

- [ ] Write tests for replacing an oversized user `LIMIT`, preserving a safe smaller limit, and leaving `REFRESH` unchanged.
- [ ] Write tests for DataFactory contains matching and page values below one.
- [ ] Run focused tests and verify they fail.
- [ ] Use SQLGlot AST transformations or a validated wrapper to enforce the maximum result size.
- [ ] Fix the impossible `key not in index` condition and normalize search inputs.
- [ ] Run focused and complete tests.
- [ ] Commit as `fix: enforce query limits and search matching`.

### Task 7: Complete API readiness, dependencies, docs, and lifecycle

**Files:**
- Modify: `main.py`
- Modify: `requirements.txt`
- Modify: `CLAUDE.md`
- Modify: `utils/hive_pool.py` and `utils/mysql_pool.py` for shutdown hooks if needed
- Test: `tests/test_app_composition.py`

- [ ] Write tests that assert FineReport REST routes are present, CORS is attached to the served app, and readiness reports unavailable dependencies.
- [ ] Run focused tests and verify they fail.
- [ ] Register `fr_router` and configure middleware on `combined_app`.
- [ ] Add liveness/readiness endpoints with dependency status and graceful shutdown cleanup.
- [ ] Pin unpinned dependencies to the versions used by the current environment.
- [ ] Update architecture, startup, and test documentation.
- [ ] Run AST parsing, all tests, dependency syntax checks, and `docker compose config --quiet`.
- [ ] Commit as `chore: complete api readiness docs and tests`.

## Final verification

- [ ] Run the complete test suite with `pytest -q`.
- [ ] Parse every Python file with `ast.parse`.
- [ ] Inspect `git diff --check` and `git status`.
- [ ] Confirm only the requested files changed and that the existing `.DS_Store` remains untouched.
