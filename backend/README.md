# Hexgent Web 后端

这是 Hexgent Agent Runtime 之外的一层 FastAPI 适配器。它只负责 HTTP、SSE 和
Session/Run 生命周期，不修改 Agent Loop、Context、Plan 或工具执行语义。

## 启动

```bash
uv run uvicorn backend.app:create_app --factory --reload
```

也可以使用：

```bash
uv run python -m backend
```

默认只允许 Session 使用服务启动目录作为 `workdir`，浏览器不能借此选择任意服务端路径。

## 核心接口

- `POST /api/sessions`：创建 Session。
- `GET /api/sessions/{session_id}`：查询 Session、当前 Run 和 Plan。
- `POST /api/sessions/{session_id}/runs`：立即创建后台 Run。
- `GET /api/runs/{run_id}`：查询 Run 状态。
- `GET /api/runs/{run_id}/events`：订阅 SSE；支持 `Last-Event-ID`。
- `POST /api/runs/{run_id}/steer`：向当前 Run 的 FIFO 邮箱提交原始 Query。
- `POST /api/runs/{run_id}/pause`：请求安全暂停。
- `POST /api/runs/{run_id}/cancel`：请求安全取消。
- `POST /api/runs/{run_id}/permissions/{request_id}`：批准或拒绝工具执行。

## 与 Java Web 开发的对应关系

- `schemas.py` 类似 DTO 与 Bean Validation；Pydantic 在进入路由前完成解析和校验。
- `routes/` 类似 Controller，只处理协议，不持有运行状态。
- `RunManager` 类似有状态的 Application Service；它维护进程内 Session/Run 聚合。
- `dependencies.py` 类似从容器取得单例 Bean，不过 FastAPI 使用函数依赖注入。
- `asyncio.to_thread()` 类似把阻塞任务投递到工作线程，避免阻塞处理 HTTP/SSE 的事件循环。
- `asyncio.Condition` 类似带条件等待的通知机制；SSE 订阅者没有新事件时休眠，有事件时被唤醒。

第一版状态和事件缓存都在单进程内存中，因此不能用多个 Uvicorn worker，也不能在进程重启后
恢复旧 Run。Session Transcript 仍由 Hexgent 自己持久化。

当前 Hexgent 的 `UserConfirmation` 只向入口暴露 `tool_name` 和权限原因，所以审批事件暂时不能
安全携带完整工具参数或规则 ID。若要满足更丰富的审批 UI，应先扩展该稳定接口，而不是让 Web
层读取工具执行内部对象。
