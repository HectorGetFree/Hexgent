# Hexgent Web 前端

React + TypeScript + Vite 实现的 Hexgent Agent Workspace。前端直接通过 HTTP 和
SSE 访问 FastAPI，不代理 `/api`。

## 开发启动

先在仓库根目录启动 Web Adapter：

```bash
uv run uvicorn hexgent.adapters.web.app:create_app --factory --reload --port 8000
```

再启动前端：

```bash
cd frontend
npm install
npm run dev
```

打开 `http://127.0.0.1:5173`。默认后端地址为 `http://127.0.0.1:8000`，可以在
前端构建环境中配置：

```bash
VITE_API_BASE_URL=https://api.hexgent.example
```

后端允许的前端来源使用以下环境变量配置，多个来源以英文逗号分隔：

```bash
HEXGENT_CORS_ALLOWED_ORIGINS=https://app.hexgent.example
```

## 代码边界

- `src/api/`：HTTP 与 SSE 协议适配，类似 Java 客户端或 Gateway Adapter。
- `src/store/`：将 AgentEvent 投影为页面状态，类似前端 Application Service。
- `src/components/`：只负责展示和用户输入，不决定 Run 领域状态。
- `src/types/`：与 FastAPI 响应相对应的 TypeScript DTO。

Pause 和 Cancel 接口只表示控制请求已接受；页面必须等待 `run_finished` 事件后才能
确认 `paused` 或 `cancelled`。EventSource 断线时由浏览器自动携带 `Last-Event-ID`
重连，后端从内存缓存继续回放事件。
