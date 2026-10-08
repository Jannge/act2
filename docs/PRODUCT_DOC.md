# pg-mcp 产品文档（优化版）

> 本文档基于源码通读（`src/pg_mcp/` 全部模块）与本次优化后状态编写，所有配置默认值、字段名、接口签名均来自代码本身，未做猜测。

---

## 1. 产品定位

**pg-mcp** 是一个生产级 **PostgreSQL 自然语言转 SQL（NL2SQL）服务**：用户用自然语言提问，系统自动生成经安全校验的 PostgreSQL SQL，并在授权范围内执行、返回结果，再用 LLM 对结果进行置信度校验。

- **解决的问题**：让不懂 SQL 的业务人员 / 分析师直接对 PostgreSQL 提问；同时把"只读、防注入、防危险函数、限流、熔断"等安全要求内建于管线，避免裸暴露数据库。
- **两种交付形态**：
  1. **MCP 服务**（`python -m pg_mcp`）：通过 stdio 对接 Claude Desktop 等 MCP 客户端。
  2. **Web 应用**（本次新增原型，`webapp/`）：可独立运行的 HTTP 服务 + 原生前端页面，开箱即用（含 Demo 模式）。

---

## 2. 整体架构

```
┌─────────────────────────────┐         ┌─────────────────────────────┐
│   MCP 客户端 / Web 前端       │  ───▶  │  接入层                      │
│  (Claude Desktop / 浏览器)    │         │  FastMCP server.py           │
│                              │         │  或 FastAPI webapp/app.py    │
└─────────────────────────────┘         └──────────────┬──────────────┘
                                                       │
                                                       ▼
                                          ┌────────────────────────┐
                                          │   QueryOrchestrator    │
                                          │ 重试 + 熔断器 + 限流    │
                                          └───┬───────┬───────┬────┘
                                              │       │       │
                       ┌──────────────────────┘       │       └──────────────────┐
                       ▼                              ▼                          ▼
               ┌───────────────┐            ┌───────────────┐          ┌───────────────────┐
               │ SQLGenerator  │            │ SQLValidator  │          │  SQLExecutor      │
               │ (OpenAI LLM)  │──SQL──▶    │ (SQLGlot 安全) │──SQL──▶   │ (asyncpg 事务)    │
               └───────────────┘            └───────────────┘          └─────────┬─────────┘
                                                                                 │
                                                                                 ▼
                                                                   ┌───────────────────────┐
                                                                   │ ResultValidator(LLM)  │
                                                                   │ 置信度评分             │
                                                                   └───────────────────────┘

支撑组件：SchemaCache（缓存 + TTL + 自动刷新）、db.pool（多库连接池）、
resilience（CircuitBreaker / MultiRateLimiter）、observability（日志/指标/追踪）
```

**单次请求流转**：解析问题 → 解析/校验目标库 → 加载 Schema 缓存 → LLM 生成 SQL（带限流）→ SQLGlot 安全校验（失败则带错误反馈重试）→ 执行（只读/读写事务、行数上限、超时）→ LLM 结果校验（置信度）→ 结构化响应。

---

## 3. 功能模块梳理

| 模块 | 职责 |
|------|------|
| `config/settings.py` | 全部配置（Pydantic + 环境变量前缀）。**新增**：`DATABASE_URLS` 多库 DSN、`SECURITY_BLOCKED_TABLES/COLUMNS`、`CACHE_AUTO_REFRESH_*`、`RESILIENCE_RATE_LIMIT_*`；放宽 `OPENAI_API_KEY` 空值校验。 |
| `cache/schema_cache.py` | Schema 缓存，支持 TTL 失效与后台自动刷新（`start_auto_refresh`）。 |
| `db/pool.py` | 连接池：`create_pool`（单库）、`create_pools`（多库）、`close_pools`。 |
| `db/introspection.py` | 通过 `information_schema` 自省表/列/类型/约束。 |
| `models/query.py` | `QueryRequest` / `QueryResponse` / `ValidationResult` / `ResultValidationResult` / `QueryResult`。`ErrorDetail` 现统一由 `models/errors.py` 提供。 |
| `models/errors.py` | `ErrorCode` 枚举与 `ErrorDetail`（pydantic 模型）、异常体系（`PgMcpError` 及其子类）。 |
| `models/schema.py` | `DatabaseSchema` / `TableSchema` / `ColumnSchema` 等结构。 |
| `services/sql_generator.py` | 调用 OpenAI 将自然语言 + Schema 转为 SQL；**现返回 `(sql, tokens_used)`**。 |
| `services/sql_validator.py` | 基于 SQLGlot 校验：仅允许 SELECT/集合操作、拦截危险函数、拦截 `blocked_tables/columns`、拦截多语句；`allow_write_operations` 时放行 INSERT/UPDATE/DELETE（DDL/DCL 仍禁）。 |
| `services/sql_executor.py` | 安全执行：设置 `statement_timeout` / `search_path` / 可选 `SET ROLE`；写操作走读写事务，其余走只读事务；行数上限与类型序列化（datetime/decimal/uuid/bytes）。 |
| `services/result_validator.py` | 用 LLM 校验结果是否匹配问题，给出 0–100 置信度与可接受判定。 |
| `services/orchestrator.py` | 编排全流程：重试 + 指数退避 + 熔断器 + **限流（LLM 与执行分别限流）** + 多库 executor 选择。 |
| `resilience/circuit_breaker.py` | 线程安全熔断器（CLOSED/OPEN/HALF_OPEN）。 |
| `resilience/rate_limiter.py` | `MultiRateLimiter`：按资源（queries / llm）分别用信号量限并发。 |
| `observability/logging.py` | 结构化日志（可选 JSON）、`SensitiveDataFilter` 过滤敏感信息。 |
| `observability/metrics.py` | Prometheus `MetricsCollector`（单例）指标收集。 |
| `observability/tracing.py` | `request_context` / `TracingLogger` / `trace_async` 请求级追踪。 |
| `server.py` | FastMCP stdio 服务：`lifespan` 统一初始化/清理，**现支持多库连接池、按配置启停自动刷新、按配置构造限流器并下发给编排器**。 |
| `webapp/`（**本次新增**） | FastAPI 后端 `app.py` + Live 后端 `live.py` + 原生前端 `index.html`；含 Demo 模式。 |

---

## 4. 本次优化清单

### 4.1 修复失效功能（必做）

1. **限流器真正生效**
   - 原状：`MultiRateLimiter` 在 `server.py` 中实例化但从未被调用，`for_queries()` / `for_llm()` 是死代码；且限流配额硬编码为 10/5。
   - 修复：将限流器注入 `QueryOrchestrator`；生成 SQL 处用 `for_llm()` 包裹、执行 SQL 处用 `for_queries()` 包裹；配额读取 `RESILIENCE_RATE_LIMIT_QUERIES` / `RESILIENCE_RATE_LIMIT_LLM`。

2. **`tokens_used` 真实回填**
   - 原状：`SQLGenerator.generate()` 返回 `str`，orchestrator 中 `tokens_used` 恒为 `None`/0，响应里无真实 token 消耗。
   - 修复：`generate()` 改为返回 `tuple[str, int | None]`，从 `response.usage.total_tokens` 提取并透传至 `QueryResponse.tokens_used`。

3. **写操作开关真正生效**
   - 原状：`SECURITY_ALLOW_WRITE_OPERATIONS` 在配置中存在，但 `server.py` 从未传给 `SQLValidator`（始终走默认 `False`），且 `SQLExecutor` 的读写事务判断已具备但无入口触发。
   - 修复：`server.py` 将 `security.allow_write_operations` 传入 `SQLValidator`；执行时写操作在读写事务中运行，其余在只读事务中运行。

4. **`blocked_tables` / `blocked_columns` 真正生效**
   - 原状：`server.py` 向 `SQLValidator` 传入 `None`，环境变量配置无效。
   - 修复：传入 `SecurityConfig.blocked_tables` / `blocked_columns`（支持 `SECURITY_BLOCKED_TABLES` / `SECURITY_BLOCKED_COLUMNS`）。

5. **Demo / 无密钥环境可启动**
   - 原状：`Settings()` 因 `OPENAI_API_KEY` 非空校验，在无密钥环境直接抛错，导致 `python -m pg_mcp` 与 Web Demo 启动即崩溃。
   - 修复：放宽为**允许空密钥**，仅在提供密钥时校验 `sk-` 前缀。密钥缺失时仅在实际调用 LLM 时才失败，便于 Demo 与网关注入凭证场景。

### 4.2 修复类型隐患

1. **`ErrorDetail` 重复定义**
   - 原状：`query.py` 与 `errors.py` 各定义一份 `ErrorDetail`，且 `query.py` 版本为普通类、与序列化不一致。
   - 修复：统一为 `errors.py` 中的 pydantic `BaseModel`；`query.py` 重新导出该定义（不影响 `orchestrator` 的 `from pg_mcp.models.query import ErrorDetail`）。

2. **`QueryResponse` 重复 `to_dict`**
   - 原状：`QueryResponse` 定义了两个 `to_dict`，后者覆盖前者并使用 `exclude_none=True`，会丢失 `tokens_used` 字段。
   - 修复：移除重复定义，保留确保 `tokens_used` 始终存在的版本。

### 4.3 多库 + 缓存刷新

1. **多库连接池**
   - 原状：仅创建单一 `create_pool(_settings.database)`，编排器只持有主库 executor，多库提问实际不可用。
   - 修复：`server.py` 通过 `Settings.build_database_configs()` + `create_pools()` 创建多库连接池；每个库用各自的 `DatabaseConfig` 创建 `SQLExecutor`；`QueryOrchestrator` 按请求中的 `database` 名选择对应 executor 执行。

2. **多库配置入口**
   - 新增环境变量 `DATABASE_URLS`：逗号分隔的 `postgresql://user:pass@host:port/db` DSN 列表，附加到主库之后。

3. **缓存自动刷新生效**
   - 原状：`start_auto_refresh` 调用被注释。
   - 修复：当 `CACHE_AUTO_REFRESH_ENABLED=True` 且 `CACHE_ENABLED=True` 时启动后台刷新，间隔读取 `CACHE_AUTO_REFRESH_INTERVAL_MINUTES`；关闭时由 `lifespan` 优雅停止。

### 4.4 文档修正（README）

- 运行入口由错误的 `python main.py`（仓库根 `main.py` 仅为"两数相加"玩具示例）更正为真实入口 `python -m pg_mcp`（`src/pg_mcp/__main__.py`）。
- `OPENAI_MODEL` 默认值由 README 宣称的 `gpt-5.2-mini` 更正为代码实际默认 `gpt-4o-mini`。
- `OPENAI_MAX_TOKENS` 默认值由 `32000` 更正为 `2000`。
- `OBSERVABILITY_LOG_FORMAT` 默认值由 `json` 更正为 `text`。

---

## 5. 配置参考（以代码实际默认值为准）

> 前缀见各小节；`Settings` 顶层字段（如 `DATABASE_URLS`）无前缀，直接对应同名环境变量。

### 5.1 通用
| 变量 | 说明 | 默认值 |
|------|------|--------|
| `DATABASE_URLS` | 逗号分隔的额外 PostgreSQL DSN（多库） | 空 |

### 5.2 数据库 `DATABASE_`
| 变量 | 说明 | 默认值 |
|------|------|--------|
| `DATABASE_HOST` | 主机 | `localhost` |
| `DATABASE_PORT` | 端口 | `5432` |
| `DATABASE_NAME` | 库名 | `postgres` |
| `DATABASE_USER` | 用户 | `postgres` |
| `DATABASE_PASSWORD` | 密码 | 空 |
| `DATABASE_MIN_POOL_SIZE` | 最小连接数 | `5` |
| `DATABASE_MAX_POOL_SIZE` | 最大连接数 | `20` |
| `DATABASE_POOL_TIMEOUT` | 获取连接超时（秒） | `30` |
| `DATABASE_COMMAND_TIMEOUT` | 命令超时（秒） | `30` |

### 5.3 OpenAI `OPENAI_`
| 变量 | 说明 | 默认值 |
|------|------|--------|
| `OPENAI_API_KEY` | API 密钥（**允许为空**，仅在使用 LLM 时校验格式） | 空 |
| `OPENAI_MODEL` | 模型 | `gpt-4o-mini` |
| `OPENAI_MAX_TOKENS` | 单次最大 token | `2000` |
| `OPENAI_TEMPERATURE` | 温度 | `0.0` |
| `OPENAI_TIMEOUT` | 请求超时（秒） | `30` |

### 5.4 安全 `SECURITY_`
| 变量 | 说明 | 默认值 |
|------|------|--------|
| `SECURITY_ALLOW_WRITE_OPERATIONS` | 放行 INSERT/UPDATE/DELETE（DDL/DCL 仍禁） | `false` |
| `SECURITY_BLOCKED_FUNCTIONS` | 危险函数黑名单（逗号分隔） | `pg_sleep,pg_read_file,pg_write_file,lo_import,lo_export` 等 |
| `SECURITY_BLOCKED_TABLES` | 禁止访问的表（逗号分隔） | 空 |
| `SECURITY_BLOCKED_COLUMNS` | 禁止访问的列（逗号分隔，支持 `表.列`） | 空 |
| `SECURITY_MAX_ROWS` | 单查询最大返回行数 | `10000` |
| `SECURITY_MAX_EXECUTION_TIME` | 查询超时（秒） | `30` |
| `SECURITY_READONLY_ROLE` | 切换到的只读角色（可选） | 空 |
| `SECURITY_SAFE_SEARCH_PATH` | 会话 `search_path` | `public` |

### 5.5 校验 `VALIDATION_`
| 变量 | 说明 | 默认值 |
|------|------|--------|
| `VALIDATION_MAX_QUESTION_LENGTH` | 问题最大长度 | `10000` |
| `VALIDATION_MIN_CONFIDENCE_SCORE` | 最小置信度阈值 | `70` |
| `VALIDATION_ENABLED` | 启用 LLM 结果校验 | `true` |
| `VALIDATION_SAMPLE_ROWS` | 参与校验的抽样行数 | `5` |
| `VALIDATION_TIMEOUT_SECONDS` | 结果校验超时（秒） | `10` |
| `VALIDATION_CONFIDENCE_THRESHOLD` | 可接受置信度阈值 | `70` |

### 5.6 缓存 `CACHE_`
| 变量 | 说明 | 默认值 |
|------|------|--------|
| `CACHE_SCHEMA_TTL` | Schema 缓存 TTL（秒） | `3600` |
| `CACHE_MAX_SIZE` | 最大缓存条目 | `100` |
| `CACHE_ENABLED` | 启用缓存 | `true` |
| `CACHE_AUTO_REFRESH_ENABLED` | **启用后台自动刷新** | `false` |
| `CACHE_AUTO_REFRESH_INTERVAL_MINUTES` | 自动刷新间隔（分钟） | `60` |

### 5.7 弹性 `RESILIENCE_`
| 变量 | 说明 | 默认值 |
|------|------|--------|
| `RESILIENCE_MAX_RETRIES` | 最大重试次数 | `3` |
| `RESILIENCE_RETRY_DELAY` | 初始重试延迟（秒） | `1.0` |
| `RESILIENCE_BACKOFF_FACTOR` | 退避倍数 | `2.0` |
| `RESILIENCE_CIRCUIT_BREAKER_THRESHOLD` | 熔断失败阈值 | `5` |
| `RESILIENCE_CIRCUIT_BREAKER_TIMEOUT` | 熔断器超时（秒） | `60` |
| `RESILIENCE_RATE_LIMIT_QUERIES` | **查询并发上限（限流）** | `10` |
| `RESILIENCE_RATE_LIMIT_LLM` | **LLM 并发上限（限流）** | `5` |

### 5.8 可观测性 `OBSERVABILITY_`
| 变量 | 说明 | 默认值 |
|------|------|--------|
| `OBSERVABILITY_METRICS_ENABLED` | 启用 Prometheus 指标 | `true` |
| `OBSERVABILITY_METRICS_PORT` | 指标端口 | `9090` |
| `OBSERVABILITY_LOG_LEVEL` | 日志级别 | `INFO` |
| `OBSERVABILITY_LOG_FORMAT` | 日志格式 | `text` |

---

## 6. Web API 说明（本次新增原型）

Web 应用在 `webapp/` 下，由 FastAPI 驱动；`GET /` 返回内置前端页面 `index.html`。

### 6.1 模式
- **Demo 模式**：设置 `PG_MCP_DEMO=1` 启动。无需 OpenAI / PostgreSQL，直接返回示例 SQL + 示例结果 + 置信度，便于演示与本地验证。
- **Live 模式**：不设置 `PG_MCP_DEMO`，按环境变量连接真实数据库与 OpenAI，复用 `pg_mcp` 全部服务组件（`webapp/live.py`）。

### 6.2 端点

`GET /api/health`
```json
{ "status": "ok", "mode": "demo" }
```

`GET /api/databases`
```json
{ "databases": ["postgres"], "default": "postgres" }
```

`POST /api/query`
请求体：
```json
{
  "question": "最近 30 天注册的用户有多少？",
  "database": null,
  "return_type": "result"   // "result" 执行并返回结果；"sql" 仅返回 SQL
}
```
成功响应（节选）：
```json
{
  "success": true,
  "generated_sql": "SELECT id, name, created_at FROM users ORDER BY created_at DESC LIMIT 10",
  "validation": { "is_valid": true, "is_select": true, "allows_data_modification": false, "uses_blocked_functions": [], "error_message": null },
  "data": { "columns": ["id","name","created_at"], "rows": [ ... ], "row_count": 2, "execution_time_ms": 1.2 },
  "confidence": 90,
  "tokens_used": 42,
  "mode": "demo"
}
```
错误：参数非法返回 `400`；Live 后端不可用时返回 `503`；业务失败在响应体内以 `success=false` + `error` 表达。

`GET /`：返回前端页面（见 `webapp/index.html`），含问题输入框、目标库下拉、返回类型切换、运行按钮与结果表格。

---

## 7. 部署

### 7.1 MCP stdio 模式
```bash
# 真实入口（非根目录 main.py）
python -m pg_mcp
# 或经 uv：
uv run python -m pg_mcp
```
配合 Claude Desktop 的 `mcpServers` 配置，`args` 为 `["-m", "pg_mcp"]`。

### 7.2 Web 模式
```bash
# Demo（无需密钥/数据库）
PG_MCP_DEMO=1 uvicorn webapp.app:app --host 0.0.0.0 --port 8000

# Live（需 asyncpg/openai/prometheus-client/fastmcp）
uvicorn webapp.app:app --host 0.0.0.0 --port 8000
```
浏览器访问 `http://127.0.0.1:8000/`。

### 7.3 Docker
沿用原仓库 `Dockerfile` 与 `docker-compose.yml`（见 README 相关章节）。

---

## 8. 已知问题与免责声明

- **依赖分层**：Demo 模式仅需 `fastapi` / `uvicorn` / `pydantic` / `pydantic-settings`；Live 模式与 `python -m pg_mcp` 需完整依赖 `asyncpg` / `openai` / `prometheus-client` / `fastmcp`。
- **Python 版本**：`pyproject.toml` 声明 `requires-python>=3.14`，但源码在 Python 3.13 下可正常编译与运行（本验证环境为 3.13.12）。如用 `uv` 安装受版本约束，可临时放宽或直接使用 `pip` 安装依赖后运行。
- **真实运行前置**：Live 模式仍需要可用的 PostgreSQL 实例与有效的 OpenAI API 密钥。
- **验证范围**：本环境已完成全部修改/新增 Python 文件的 `py_compile` 语法校验，并启动 Demo 模式实际验证了 `/api/health`、`/api/databases`、`POST /api/query`（result / sql / 非法参数）与首页加载。受沙箱网络与重依赖限制，未在本环境跑通完整 `pytest` 单测与 Live 端到端链路；Live 路径代码已按真实签名接入，建议在具备 PostgreSQL + OpenAI 的环境中再做一次端到端验证。
