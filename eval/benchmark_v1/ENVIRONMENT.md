# B1-B Trial 环境

本模块只实现 HexWorkspace 的 Provision → Seed → Readiness → Observe → Archive → Destroy。真实验收入口是 `verify_environment_real.py`，不执行 Agent、真实任务、消融或完整 Benchmark Runner。

## 部署与来源

`environment.py` 依据 HexWorkspace `docs/devops/docker-compose.yml` 的现有 MySQL 8.0、Redis 7.2、MinIO RELEASE.2024-12-18T13-15-44Z、RabbitMQ 3.13-management、Qdrant v1.12.1 镜像，生成独立 Compose 项目。不能直接叠加开发 Compose，因为它固定了 container_name、共享端口且 RabbitMQ 没有独立持久化 Volume。

每个 Trial 都有独立 Compose project、bridge network、五个存储 Volume、Workspace、Runtime 和宿主 workdir。只对必要 HTTP 服务发布动态端口，监听 127.0.0.1；MySQL、Redis、AMQP 无宿主端口。配置中的业务依赖只指向该网络内服务。拒绝远端 Docker、任意业务 URL、未知 Baseline、同名既有项目、被修改的资源身份、端点或 Compose。

MySQL 仅挂载当前仓库的 `001-init-hex-workspace.sql` 副本。该完整新库脚本已有当前 19 张表及版本／索引／幂等结构；`upgrade/` 是旧库增量迁移，不能重复应用到新库。不会清空已有表或复用共享数据库。

Workspace 和 Runtime 镜像由冻结 Git 源码副本构建，并按镜像 ID 部署。Production target、POM 与源码不变。当前只读 Maven 缓存缺少默认测试／打包插件依赖，构建脚本仅在副本关闭测试阶段、固定 API 构建插件和使用 Boot 2.7.12 打包插件；这些覆盖随 build-metadata 保存，业务依赖与源码没有替换。构建不是 Maven 测试验收。

Runtime 不复制 `.env`，不配置 OpenAI Provider 凭证。Workspace 只显式注入 Embedding key，用于固定文档的真实索引。创建 Session 不 StartRun。Runtime workdir 位于不同 Trial 的私有目录，因此 `.hexgent/memory`、`.hexgent/sessions/<session_id>/transcript.jsonl`、Checkpoint 等路径隔离；这不是 T05 的多身份 Memory 安全性结论。

## 入口

```python
manager = TrialEnvironmentManager(
    root, workspace_source, workspace_image_id, runtime_image_id,
    LocalDocker(local_unix_socket),
    suite_id=contract.suite_id, case_id=contract.case_id,
    embedding_key=embedding_key,
)
environment = manager.provision(trial_id, contract.baseline_id)
# 先只确认基础部署。只有明确 require_seed=False 才允许未 Seed 的基础 PASS。
manager.readiness(environment, require_seed=False, require_vector=False)
try:
    manager.seed(environment, contract.baseline_id)
    readiness = manager.readiness(environment)
    observation = manager.observe(environment)
    manager.archive(environment)
finally:
    manager.destroy(environment)
```

Provision 失败时，可从同一 Manager 的 `environments[trial_id]` 获取已登记句柄进入清理。一个 Manager 不重复 provision 相同 Trial ID。自动重建使用新 Trial；不把重试当成原环境的干净重置。当前不提供跨进程重新接管或通用 GC。

`seed` 只接受固定版本 `b1b-seed-v1`，创建一个独立用户、默认 Workspace 和两篇 Markdown。Seed 通过真实注册／登录／目录／文档 API，绝不直接写 DocumentVersion、Parse 或 Index 状态。Baseline 文件保存明确事实、文件名和版本，SHA-256 随环境绑定。Seed 只能在无文档环境执行一次。

## 独立观察与就绪

观察器只使用固定 SELECT、MinIO 签名 GET、Qdrant generation 精确点数和 RabbitMQ 管理读接口。不会读取 Agent 自评或修改业务状态。

就绪要求：

- 真实 MySQL Schema、Redis、MinIO、Qdrant、RabbitMQ、Workspace 和 Runtime 可访问；Workspace Seed 后使用身份认证检查。
- Seed 文件集合、正式版本、原对象内容摘要与 Baseline 一致。
- 精确版本 Parse Artifact 状态 READY，MinIO 解析对象摘要一致。
- 当前 Active Generation 绑定当前 version/revision，Chunk Artifact 摘要一致、Chunk 数量为正、Qdrant generation 点数一致。
- 上传、索引、Outbox 和 MQ 队列没有未完成初始化任务。

Readiness 使用明确超时和轮询间隔，单次 Docker/HTTP 操作也有超时；到达轮询期限后不启动下一轮，当前一轮最多增加各操作的有界耗时。基础、正式版本、解析、向量的布尔结果分别保存。缺 Embedding key 或真实索引失败不能伪造 READY。环境未就绪是 INVALID；集成报告中依赖不可用为 BLOCKED，真实隔离断言失败为 FAIL。

状态摘要来自数据库实际文档、版本和 Parse/Index 状态，排除 UUID、对象路径、时间等非业务差异，支持跨 Trial 初始状态等价与污染比较。当前仅为固定 Seed 提供环境观测，不实现 T01–T03 业务评分。

## Artifact 与凭证

```text
<root>/artifacts/<suite_id>/<case_id>/<trial_id>/
  build-metadata.json
  environment-config.json
  baseline.json
  seed.json
  readiness.json
  observations/environment.json
  observations/business-state.json
  lifecycle.jsonl
  diagnostics.json
  sessions/<session_id>/...           # 若曾创建 Runtime Session
```

目录和 Suite/Case/Trial/Baseline 标识沿用 B1-A；Readiness 和 Business State 可作为 B1-A Observation 的独立数据源与证据引用。TrialManifest 仍由后续 Runner 维护，不由环境管理器虚构不存在的 Session 或 Provider Usage。

随机数据库／MinIO／MQ／JWT／Integration／Capability／Callback／Evaluation 凭证，以及登录 Token，只存 `<root>/private/<project>/`，该目录 chmod 0700，秘密文件 chmod 0600。私有配置不作为 Artifact，不原样保存 Docker Env 或服务日志。Archive 白名单保存资源标识、实际镜像 ID、配置摘要、基线、只读状态和容器健康／退出码。

销毁前校验当前项目全部资源的 Trial/owner 标签与 Compose 摘要，只对本次项目执行 down --volumes。销毁后核对资源不存在，删除本次生成的凭证；宿主 workdir 和配置模板保留作为本地历史路径，未来 Trial 不复用。重复销毁不再次执行 Docker 删除。不会执行 docker prune 或删除共享资源。

## 验收命令

```sh
.venv/bin/python -m unittest eval.benchmark_v1.test_contracts eval.benchmark_v1.test_environment -q

.venv/bin/python -m eval.benchmark_v1.build_environment_images \
  --workspace-source /Users/hector/dev/hex-workspace \
  --root <新的构建目录> \
  --docker-socket unix:///Users/hector/.docker/run/docker.sock \
  --maven-cache /Users/hector/.m2/repository

.venv/bin/python -m eval.benchmark_v1.verify_environment_real \
  --workspace-source /Users/hector/dev/hex-workspace \
  --root <新的验收目录> \
  --build-metadata <构建目录>/build-metadata.json \
  --docker-socket unix:///Users/hector/.docker/run/docker.sock \
  --embedding-env-file <私有的 Embedding 配置文件>
```

真实入口执行 A 重建、B 双部署实际写入隔离、C 省略 Seed 的初始化失败清理、D 真实 Runtime Session 与本地路径检查。Checkpoint marker 只证明路径隔离，不宣称真实 Checkpoint Restore 成功。每个 Trial 的配置与依赖都是本轮新建；不会运行共享开发部署。

本轮到 B1-B 停止。B1-C 再定义真实任务、可信业务语义断言、Provider Usage 采集与运行协议。
