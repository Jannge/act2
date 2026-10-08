# pg-mcp 产品介绍手册

> 面向后续使用 / 运维人员。读完即可安装、配置并运行本产品，包括新增的 Web 应用与 Demo 模式。

---

## 一、这是什么

**pg-mcp** 让你用自然语言查询 PostgreSQL 数据库。例如输入"最近 30 天注册的用户有多少？"，它会：

1. 生成一条安全的 PostgreSQL SQL；
2. 做安全校验（只允许读、拦截危险函数、限制行数与时长）；
3. 执行并返回结果，再用 LLM 给出结果置信度。

它有两种使用方式：

| 形态 | 入口 | 适合场景 |
|------|------|----------|
| MCP 服务 | `python -m pg_mcp` | 接入 Claude Desktop 等 MCP 客户端 |
| Web 应用 | `uvicorn webapp.app:app` | 浏览器直接使用，含可视化界面与 Demo 模式 |

---

## 二、安装

### 方式 A：uv（仓库推荐）
```bash
git clone https://github.com/Jannge/act2.git
cd act2
uv sync                       # 安装运行依赖
uv sync --all-extras         # 含开发依赖（pytest/ruff/mypy）
```

### 方式 B：pip + 虚拟环境
```bash
cd act2
python -m venv .venv
source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -e .
```

> 说明：项目 `pyproject.toml` 声明需要 Python ≥ 3.14，但源码在 3.13 亦可运行。若 `uv` 因版本约束报错，可改用 `pip install -e .` 或放宽版本约束。

### Web 应用额外可选依赖（Demo 模式专用，极轻量）
```bash
pip install fastapi uvicorn pydantic pydantic-settings
```
- **Demo 模式**只需上面 4 个包。
- **Live 模式 / MCP 服务**还需：`asyncpg openai prometheus-client fastmcp`。

---

## 三、配置

复制环境模板并编辑：
```bash
cp .env.example .env
```

`.env` 关键项（完整列表见《产品文档》第 5 节，以下为常用项）：

```bash
# 数据库（主库）
DATABASE_HOST=localhost
DATABASE_PORT=5432
DATABASE_NAME=your_database
DATABASE_USER=your_user
DATABASE_PASSWORD=your_password

# 多库（可选）：逗号分隔的完整 DSN
DATABASE_URLS=postgresql://user2:pass2@host2:5432/db2

# OpenAI（Live 模式必填；Demo 模式留空即可）
OPENAI_API_KEY=sk-xxxx
OPENAI_MODEL=gpt-4o-mini

# 安全
SECURITY_ALLOW_WRITE_OPERATIONS=false
SECURITY_BLOCKED_TABLES=secret_users,audit_log
SECURITY_BLOCKED_COLUMNS=users.password,users.salary
SECURITY_MAX_ROWS=10000
SECURITY_MAX_EXECUTION_TIME=30

# 缓存自动刷新（可选）
CACHE_AUTO_REFRESH_ENABLED=false
CACHE_AUTO_REFRESH_INTERVAL_MINUTES=60

# 限流（现已真实生效）
RESILIENCE_RATE_LIMIT_QUERIES=10
RESILIENCE_RATE_LIMIT_LLM=5
```

---

## 四、运行

### 4.1 MCP 服务（stdio）
```bash
python -m pg_mcp
```
> ⚠️ 注意：仓库根目录的 `main.py` 是"两数相加"的**玩具示例**，不是本产品入口。真实入口是 `python -m pg_mcp`（`src/pg_mcp/__main__.py`）。

接入 Claude Desktop：在 `claude_desktop_config.json` 的 `mcpServers` 中配置：
```json
{
  "mcpServers": {
    "postgres": {
      "command": "uv",
      "args": ["--directory", "/绝对路径/act2", "run", "python", "-m", "pg_mcp"],
      "env": { "DATABASE_HOST": "localhost", "DATABASE_NAME": "your_db", "OPENAI_API_KEY": "sk-xxxx" }
    }
  }
}
```

### 4.2 Web 应用 —— Demo 模式（无需密钥 / 数据库）
```bash
cd act2
PG_MCP_DEMO=1 uvicorn webapp.app:app --host 0.0.0.0 --port 8000
```
浏览器打开 `http://127.0.0.1:8000/`，即可看到界面并直接提问。Demo 模式返回的是**示例** SQL 与结果，用于体验交互与界面。

### 4.3 Web 应用 —— Live 模式（真实查询）
```bash
cd act2
uvicorn webapp.app:app --host 0.0.0.0 --port 8000
```
需先按第三节配置好数据库与 `OPENAI_API_KEY`。此时页面提问会真实生成 SQL 并查询数据库。

---

## 五、使用流程（Web 前端）

1. 打开 `http://127.0.0.1:8000/`，右上角显示当前模式（`Demo 模式` / `Live 模式`）。
2. 在文本框输入自然语言问题，例如：
   - "最近 30 天注册的用户有多少？"
   - "按销售额排序的前 10 个产品"
3. 选择**目标数据库**（多库时从下拉选择；单库留空即默认库）。
4. 选择返回类型：
   - **执行并返回结果**（默认）：生成 SQL 并查询，展示结果表格。
   - **仅生成 SQL**：只返回 SQL，不执行。
5. 点击 **运行查询**。
6. 页面依次展示：
   - 生成的 SQL（深色代码块）；
   - 查询结果表格（含列名、行数据，`NULL` 显式标注）；
   - 元信息：置信度、Token 用量、行数、耗时。
7. 失败时页面展示错误 JSON（参数错误返回 `400`；Live 后端不可用返回 `503`）。

### 直接用 API（无需前端）
```bash
# 健康检查
curl http://127.0.0.1:8000/api/health

# 已配置数据库列表
curl http://127.0.0.1:8000/api/databases

# 执行查询
curl -X POST http://127.0.0.1:8000/api/query \
  -H "Content-Type: application/json" \
  -d '{"question":"最近30天注册的用户有多少？","return_type":"result"}'
```

---

## 六、故障排查

| 现象 | 可能原因 | 处理 |
|------|----------|------|
| `python -m pg_mcp` 启动即报错 | 旧文档让你跑了 `python main.py` | 改用 `python -m pg_mcp` |
| Web 启动报缺包 | 未装 Web 依赖 | `pip install fastapi uvicorn pydantic pydantic-settings` |
| `/api/query` 返回 503 | Live 模式但缺 `asyncpg`/`openai` 等 | 装完整依赖，或用 `PG_MCP_DEMO=1` 跑 Demo |
| `OPENAI_API_KEY` 格式错误 | 不以 `sk-` 开头 | 检查密钥；Demo 模式可留空 |
| 查询报 `SECURITY_VIOLATION` | SQL 含危险函数/写操作/被禁表列 | 检查 `SECURITY_BLOCKED_*`；确需写操作则开 `SECURITY_ALLOW_WRITE_OPERATIONS=true`（仍需 DDL/DCL 被禁） |
| `Database 'xxx' not found` | 多库时库名不在配置中 | 在 `DATABASE_URLS` 或主库配置中加入该库 |
| 查询超时 | SQL 太慢 | 调大 `SECURITY_MAX_EXECUTION_TIME`，或优化索引 |
| Schema 缓存过期 | 表结构变更后缓存未刷新 | 设 `CACHE_AUTO_REFRESH_ENABLED=true`，或重启服务重载 |

---

## 七、常见问题（FAQ）

**Q1：Demo 模式的结果真实吗？**
不是。Demo 模式返回的是写死的示例 SQL 与示例数据，仅用于演示界面与交互；要查真实数据请使用 Live 模式。

**Q2：支持写操作（INSERT/UPDATE/DELETE）吗？**
默认禁止。设置 `SECURITY_ALLOW_WRITE_OPERATIONS=true` 可放行 DML，但仍禁止 DDL/DCL（CREATE/DROP/ALTER/GRANT 等），且写操作在读写事务中执行。强烈建议配合只读库用户使用。

**Q3：能同时查多个数据库吗？**
能。通过 `DATABASE_URLS` 追加 DSN，提问时在界面下拉或 API 的 `database` 字段指定库名；单库时可不指定。

**Q4：限流和熔断有什么用？**
限流（`RESILIENCE_RATE_LIMIT_*`）控制并发的查询/LLM 调用数，防资源耗尽；熔断在 LLM 连续失败时临时停止调用，防雪崩。两者均已在本版真正生效。

**Q5：如何看监控指标？**
开启 `OBSERVABILITY_METRICS_ENABLED=true` 后，Prometheus 指标暴露在 `OBSERVABILITY_METRICS_PORT`（默认 9090），`curl http://localhost:9090/metrics` 可查。

**Q6：日志里有敏感信息吗？**
默认经 `SensitiveDataFilter` 过滤；`OBSERVABILITY_LOG_FORMAT` 可选 `text`/`json`。

---

## 八、安全建议（生产环境）

1. 使用**只读数据库用户**（仅 `SELECT` 权限），必要时用 `SECURITY_READONLY_ROLE` 切换角色。
2. `OPENAI_API_KEY` 通过环境变量 / 密钥管理注入，切勿提交到版本库。
3. 通过 `SECURITY_BLOCKED_TABLES` / `SECURITY_BLOCKED_COLUMNS` 屏蔽敏感表/列（如 `users.password`）。
4. 网络隔离 + IP 限制数据库访问；开启指标与日志监控并设置异常告警。
5. 合理设置限流参数，防止滥用。
