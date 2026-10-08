"""pg-mcp FastAPI Web 应用。

在 pg_mcp 自然语言转 SQL 管线之上提供可运行的 Web 界面与 JSON API。

两种运行模式：
- Demo 模式（默认当设置 PG_MCP_DEMO=1，或缺少完整依赖时启用）：
  无需 OpenAI / PostgreSQL，直接返回示例 SQL + 示例结果 + 置信度。
- Live 模式：复用 pg_mcp 服务（SQLGenerator -> SQLValidator -> SQLExecutor
  -> ResultValidator），通过 QueryOrchestrator 对真实数据库执行查询。
  需要安装完整依赖：asyncpg、openai、prometheus-client、fastmcp。

启动：
    uvicorn webapp.app:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

# 将仓库根目录与 src 目录加入 sys.path，便于导入 pg_mcp 与 webapp 包。
_REPO = Path(__file__).resolve().parents[1]
_SRC = _REPO / "src"
for _p in (_REPO, _SRC):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from pg_mcp.config.settings import Settings  # noqa: E402
from pg_mcp.models.query import QueryRequest, ReturnType  # noqa: E402

app = FastAPI(title="pg-mcp Web", version="0.2.1")

DEMO = os.getenv("PG_MCP_DEMO", "").lower() in ("1", "true", "yes")


class QueryBody(BaseModel):
    """Web 查询请求体。"""

    question: str
    database: str | None = None
    return_type: str = "result"  # "sql" 或 "result"


@app.get("/api/health")
async def health() -> dict:
    """健康检查，返回当前运行模式。"""
    return {"status": "ok", "mode": "demo" if DEMO else "live"}


@app.get("/api/databases")
async def databases() -> dict:
    """返回已配置的数据库名称列表。"""
    settings = Settings()
    names = [c.name for c in settings.build_database_configs()]
    return {"databases": names, "default": names[0] if names else None}


@app.post("/api/query")
async def query(body: QueryBody) -> dict:
    """执行自然语言查询，返回结构化结果。"""
    if body.return_type not in ("sql", "result"):
        raise HTTPException(status_code=400, detail="return_type 必须是 'sql' 或 'result'")
    if not body.question or not body.question.strip():
        raise HTTPException(status_code=400, detail="question 不能为空")
    if DEMO:
        return _demo_response(body)
    return await _live_query(body)


def _demo_response(body: QueryBody) -> dict:
    """构造 Demo 模式的示例响应（不依赖 OpenAI / PostgreSQL）。"""
    question = body.question.strip()
    lowered = question.lower()
    if "user" in lowered or "用户" in question:
        sql = "SELECT id, name, email, created_at FROM users ORDER BY created_at DESC LIMIT 10"
    elif "count" in lowered or "多少" in question or "统计" in question:
        sql = "SELECT COUNT(*) AS cnt FROM orders"
    else:
        sql = "SELECT * FROM information_schema.tables WHERE table_schema = 'public' LIMIT 20"

    data = None
    if body.return_type == "result":
        data = {
            "columns": ["id", "name", "created_at"],
            "rows": [
                {"id": 1, "name": "Alice", "created_at": "2024-01-01T10:00:00"},
                {"id": 2, "name": "Bob", "created_at": "2024-02-15T09:30:00"},
            ],
            "row_count": 2,
            "execution_time_ms": 1.2,
        }

    return {
        "success": True,
        "generated_sql": sql,
        "validation": {
            "is_valid": True,
            "is_select": True,
            "allows_data_modification": False,
            "uses_blocked_functions": [],
            "error_message": None,
        },
        "data": data,
        "error": None,
        "confidence": 90,
        "tokens_used": 42,
        "mode": "demo",
    }


async def _live_query(body: QueryBody) -> dict:
    """调用 Live 后端执行真实查询。"""
    try:
        from webapp.live import LiveBackend
    except Exception as exc:  # pragma: no cover - 依赖缺失时降级提示
        raise HTTPException(
            status_code=503,
            detail=(
                f"Live 后端不可用：{exc!s}。"
                "请安装完整依赖（asyncpg、openai、prometheus-client、fastmcp），"
                "或使用 PG_MCP_DEMO=1 启动 Demo 模式。"
            ),
        )
    backend = LiveBackend.instance()
    result = await backend.run(body.question, body.database, body.return_type)
    result["mode"] = "live"
    return result


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    """返回内置 Web 前端页面。"""
    html_path = Path(__file__).resolve().parent / "index.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))
