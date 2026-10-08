"""pg-mcp Web 应用的 Live 后端。

复用 pg_mcp 全部服务组件（SQLGenerator -> SQLValidator -> SQLExecutor
-> ResultValidator），通过 QueryOrchestrator 对真实 PostgreSQL 执行查询。

需要完整依赖：asyncpg、openai、prometheus-client、fastmcp。
首次请求时懒加载连接池与编排器，避免阻塞进程启动。
"""

from __future__ import annotations

import asyncio

from pg_mcp.cache.schema_cache import SchemaCache
from pg_mcp.config.settings import Settings
from pg_mcp.db.pool import create_pools
from pg_mcp.models.query import QueryRequest, ReturnType
from pg_mcp.resilience.rate_limiter import MultiRateLimiter
from pg_mcp.services.orchestrator import QueryOrchestrator
from pg_mcp.services.result_validator import ResultValidator
from pg_mcp.services.sql_executor import SQLExecutor
from pg_mcp.services.sql_generator import SQLGenerator
from pg_mcp.services.sql_validator import SQLValidator


class LiveBackend:
    """单例 Live 后端，持有连接池与 QueryOrchestrator。"""

    _instance: "LiveBackend | None" = None
    _lock: asyncio.Lock = asyncio.Lock()

    def __init__(self) -> None:
        self.settings: Settings = Settings()
        self._pools: dict | None = None
        self._orchestrator: QueryOrchestrator | None = None
        self._ready: bool = False

    @classmethod
    def instance(cls) -> "LiveBackend":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def ensure_ready(self) -> None:
        """幂等地初始化连接池与编排器（线程/协程安全）。"""
        if self._ready:
            return
        async with self._lock:
            if self._ready:
                return
            await self._setup()
            self._ready = True

    async def _setup(self) -> None:
        settings = self.settings
        db_configs = settings.build_database_configs()
        self._pools = await create_pools(db_configs)

        schema_cache = SchemaCache(settings.cache)
        for name, pool in self._pools.items():
            await schema_cache.load(name, pool)

        sql_generator = SQLGenerator(settings.openai)
        sql_validator = SQLValidator(
            config=settings.security,
            blocked_tables=settings.security.blocked_tables,
            blocked_columns=settings.security.blocked_columns,
            allow_write_operations=settings.security.allow_write_operations,
        )
        executors: dict[str, SQLExecutor] = {}
        for cfg in db_configs:
            executors[cfg.name] = SQLExecutor(
                pool=self._pools[cfg.name],
                security_config=settings.security,
                db_config=cfg,
            )
        result_validator = ResultValidator(
            openai_config=settings.openai,
            validation_config=settings.validation,
        )
        rate_limiter = MultiRateLimiter(
            query_limit=settings.resilience.rate_limit_queries,
            llm_limit=settings.resilience.rate_limit_llm,
        )
        self._orchestrator = QueryOrchestrator(
            sql_generator=sql_generator,
            sql_validator=sql_validator,
            sql_executors=executors,
            result_validator=result_validator,
            schema_cache=schema_cache,
            pools=self._pools,
            resilience_config=settings.resilience,
            validation_config=settings.validation,
            rate_limiter=rate_limiter,
        )

    async def run(self, question: str, database: str | None, return_type: str) -> dict:
        """执行一次查询并返回结构化字典。"""
        await self.ensure_ready()
        request = QueryRequest(
            question=question,
            database=database,
            return_type=ReturnType(return_type),
        )
        response = await self._orchestrator.execute_query(request)
        return response.to_dict()
