"""B1-B 专用 Compose 生命周期；不接收外部业务 URL，不修改 Production。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import json
import hmac
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import time
from urllib.parse import quote, urlparse
from typing import Any

import httpx
from pydantic import TypeAdapter

from .contracts import Identifier


BASELINE = Path(__file__).with_name("environment_baseline.json")
LABEL = "com.hexgent.benchmark"
SERVICES = ("mysql", "redis", "minio", "rabbitmq", "qdrant", "workspace", "runtime")
STORES = ("mysql", "redis", "minio", "rabbitmq", "qdrant")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def dump(path: Path, value: Any):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


class EnvironmentError(RuntimeError):
    """环境错误统一归为实验 INVALID；不包含原始命令、响应或凭证。"""


class LocalDocker:
    def __init__(self, socket: str):
        # 禁止 TCP / SSH / 远端 Docker；即使进程有 DOCKER_HOST 也不使用它。
        if not socket.startswith("unix:///"):
            raise EnvironmentError("仅允许显式本地 Unix Docker Socket")
        self.socket = socket

    def run(self, args: list[str], *, input: str | None = None, timeout: float = 60) -> str:
        env = {k: v for k, v in os.environ.items() if not k.startswith("DOCKER_")}
        try:
            result = subprocess.run(["docker", "--host", self.socket, *args], input=input,
                                    text=True, capture_output=True, timeout=timeout, env=env)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise EnvironmentError("Docker 不可用或命令超时") from exc
        if result.returncode:
            raise EnvironmentError(f"Docker 操作失败：{args[0]}，exit={result.returncode}")
        return result.stdout


@dataclass
class TrialEnvironment:
    trial_id: str
    baseline_id: str
    project: str
    owner: str
    artifact_root: Path
    private_root: Path
    compose_sha256: str
    baseline_sha256: str
    endpoints: dict[str, str] = field(default_factory=dict)
    seeded: bool = False
    destroyed: bool = False

    @property
    def workdir(self) -> Path:
        return self.private_root / "workdir"


class TrialEnvironmentManager:
    """资源只由本类生成；Suite/Case 固定后 provision 使用 B1-A 的 Trial/Baseline ID。"""

    def __init__(self, root: Path, workspace_source: Path, workspace_image: str,
                 runtime_image: str, docker: LocalDocker, *, suite_id="environment-smoke-v1",
                 case_id="lifecycle", embedding_key: str = ""):
        for identifier in (suite_id, case_id):
            TypeAdapter(Identifier).validate_python(identifier)
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.:/-]+", workspace_image) or not re.fullmatch(
                r"[a-z0-9][a-z0-9_.:/-]+", runtime_image):
            raise EnvironmentError("镜像标识无效")
        self.root = root.resolve()
        self.source = workspace_source.resolve()
        self.workspace_image, self.runtime_image = workspace_image, runtime_image
        self.docker, self.embedding_key = docker, embedding_key
        self.suite_id, self.case_id = suite_id, case_id
        self.environments: dict[str, TrialEnvironment] = {}
        self._ownership: dict[str, tuple] = {}
        self._endpoints: dict[str, dict[str, str]] = {}
        self.baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
        self.baseline_digest = sha256(BASELINE.read_bytes()).hexdigest()
        self.lifecycle_code_digest = sha256(Path(__file__).read_bytes()).hexdigest()

    def _event(self, environment, action, status, **details):
        record = dict(schema_version=1, suite_id=self.suite_id, case_id=self.case_id,
                      trial_id=environment.trial_id, baseline_id=environment.baseline_id,
                      time=now(), action=action, status=status, **details)
        path = environment.artifact_root / "lifecycle.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _guard(self, environment, *, destroyed_ok=False):
        if self.environments.get(environment.trial_id) is not environment:
            raise EnvironmentError("环境不是当前 Manager 持有的 Trial")
        identity = (environment.project, environment.owner, environment.private_root,
                    environment.artifact_root, environment.compose_sha256, environment.baseline_sha256)
        if self._ownership.get(environment.trial_id) != identity:
            raise EnvironmentError("Trial 资源身份已被修改")
        if environment.trial_id in self._endpoints and environment.endpoints != self._endpoints[environment.trial_id]:
            raise EnvironmentError("Trial 服务地址已被修改，拒绝访问外部目标")
        if environment.destroyed and not destroyed_ok:
            raise EnvironmentError("Trial 已销毁")
        expected = self.root / "private" / environment.project
        if environment.private_root != expected or expected.is_symlink():
            raise EnvironmentError("私有环境路径归属不匹配")
        if not environment.destroyed:
            path = expected / "compose.json"
            if path.is_symlink() or sha256(path.read_bytes()).hexdigest() != environment.compose_sha256:
                raise EnvironmentError("Compose 配置已被修改，拒绝执行")

    def _compose(self, environment, *args, timeout=60):
        self._guard(environment)
        return self.docker.run(["compose", "--project-name", environment.project,
                                "--file", str(environment.private_root / "compose.json"),
                                *args], timeout=timeout)

    def _resources(self, environment):
        found = {}
        selector = f"label={LABEL}.owner={environment.owner}"
        for kind in ("container", "volume", "network"):
            ids = self.docker.run([kind, "ls", "--quiet", "--filter", selector]).split()
            records = json.loads(self.docker.run([kind, "inspect", *ids])) if ids else []
            for item in records:
                labels = item.get("Labels") or (item.get("Config") or {}).get("Labels") or {}
                if labels.get(f"{LABEL}.trial") != environment.trial_id or labels.get(
                        "com.docker.compose.project") != environment.project:
                    raise EnvironmentError("Docker 资源标签不匹配")
            found[kind] = records
        return found

    def _container(self, environment, service):
        self._guard(environment)
        ids = self._compose(environment, "ps", "--all", "--quiet", service).split()
        matches = json.loads(self.docker.run(["container", "inspect", *ids])) if ids else []
        if len(matches) != 1:
            raise EnvironmentError(f"Trial 服务未唯一存在：{service}")
        labels = matches[0]["Config"]["Labels"]
        if labels.get(f"{LABEL}.owner") != environment.owner or labels.get(f"{LABEL}.trial") != environment.trial_id:
            raise EnvironmentError("服务资源归属不匹配")
        return matches[0]["Id"]

    def _exec(self, environment, service, args, *, input=None):
        return self.docker.run(["exec", "-i", self._container(environment, service), *args], input=input)

    def _sql(self, environment, statement):
        # 观察器只接受 SELECT；不提供通用 SQL 初始化／清理接口。
        if not statement.startswith("SELECT ") or ";" in statement:
            raise EnvironmentError("观察 SQL 仅允许单条 SELECT")
        raw = self._exec(environment, "mysql", ["sh", "-c",
            'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -uroot --batch --raw --skip-column-names hex_workspace'],
            input=statement + "\n")
        return [json.loads(line) for line in raw.splitlines() if line]

    def provision(self, trial_id: str, baseline_id: str) -> TrialEnvironment:
        TypeAdapter(Identifier).validate_python(trial_id)
        if baseline_id != self.baseline["baseline_id"]:
            raise EnvironmentError("未知 Baseline")
        if trial_id in self.environments:
            raise EnvironmentError("Trial ID 已存在，重建必须使用新的 Trial ID")
        project = "hgb1b-" + sha256(f"{self.suite_id}/{self.case_id}/{trial_id}".encode()).hexdigest()[:16]
        private = self.root / "private" / project
        artifacts = self.root / "artifacts" / self.suite_id / self.case_id / trial_id
        private.mkdir(parents=True, exist_ok=False, mode=0o700)
        artifacts.mkdir(parents=True, exist_ok=False)
        (private / "workdir").mkdir()
        owner = secrets.token_hex(16)
        values = {key: secrets.token_hex(24) for key in ["DB_PASSWORD", "MINIO_PASSWORD",
                   "MQ_PASSWORD", "INTEGRATION_TOKEN", "CAPABILITY_TOKEN", "CALLBACK_TOKEN", "EVALUATION_TOKEN",
                   "USER_PASSWORD", "JWT_SECRET"]}
        values["DASHSCOPE_API_KEY"] = self.embedding_key
        dump(private / "credentials.json", values)
        (private / "credentials.json").chmod(0o600)
        for service, variables in {
                "mysql": {"MYSQL_ROOT_PASSWORD": values["DB_PASSWORD"]},
                "minio": {"MINIO_ROOT_USER": "benchmark", "MINIO_ROOT_PASSWORD": values["MINIO_PASSWORD"]},
                "rabbitmq": {"RABBITMQ_DEFAULT_USER": "benchmark", "RABBITMQ_DEFAULT_PASS": values["MQ_PASSWORD"]},
                "workspace": {"DB_PASSWORD": values["DB_PASSWORD"], "MINIO_PASSWORD": values["MINIO_PASSWORD"],
                              "MQ_PASSWORD": values["MQ_PASSWORD"], "DASHSCOPE_API_KEY": self.embedding_key,
                              "JWT_SECRET": values["JWT_SECRET"],
                              "HEXGENT_WORKSPACE_INTEGRATION_TOKEN": values["INTEGRATION_TOKEN"],
                              "HEXGENT_WORKSPACE_CAPABILITY_TOKEN": values["CAPABILITY_TOKEN"],
                              "HEXGENT_WORKSPACE_LIFECYCLE_TOKEN": values["CALLBACK_TOKEN"],
                              "EVALUATION_TOKEN": values["EVALUATION_TOKEN"]},
                "runtime": {"HEXGENT_WORKSPACE_URL": "http://workspace:8098",
                            "HEXGENT_WORKSPACE_INTEGRATION_TOKEN": values["INTEGRATION_TOKEN"],
                            "HEXGENT_WORKSPACE_CAPABILITY_TOKEN": values["CAPABILITY_TOKEN"],
                            "HEXGENT_WORKSPACE_LIFECYCLE_TOKEN": values["CALLBACK_TOKEN"],
                            "HEXGENT_WORKSPACE_LIFECYCLE_URL": "http://workspace:8098/internal/v1/agent-runs/lifecycle",
                            "HEXGENT_WORKSPACE_PERMISSION_URL": "http://workspace:8098/internal/v1/agent-runs/permissions"},
                }.items():
            (private / f"{service}.env").write_text("".join(f"{k}={v}\n" for k, v in variables.items()))
            (private / f"{service}.env").chmod(0o600)
        sql = self.source / "docs/devops/mysql/sql/001-init-hex-workspace.sql"
        shutil.copyfile(sql, private / "init.sql")
        (private / "application-benchmark.yml").write_text(WORKSPACE_CONFIG)
        config = self._configuration(private, trial_id, owner)
        dump(private / "compose.json", config)
        environment = TrialEnvironment(trial_id, baseline_id, project, owner, artifacts, private,
            sha256((private / "compose.json").read_bytes()).hexdigest(), self.baseline_digest)
        self.environments[trial_id] = environment
        self._ownership[trial_id] = (environment.project, environment.owner, environment.private_root,
                                    environment.artifact_root, environment.compose_sha256, environment.baseline_sha256)
        self._event(environment, "provision", "STARTED")
        try:
            # 不接管既有同名项目；私有状态的名字由 Trial 唯一确定。
            for kind in ("container", "volume", "network"):
                if self.docker.run([kind, "ls", "--quiet", "--filter", f"label=com.docker.compose.project={project}"]).strip():
                    raise EnvironmentError("Trial Compose 项目已存在，拒绝复用")
            self._compose(environment, "up", "--detach", "--pull", "never", timeout=180)
            for service, port in {"workspace": 8098, "runtime": 8000, "minio": 9000,
                                  "qdrant": 6333, "rabbitmq": 15672}.items():
                binding = self._compose(environment, "port", service, str(port)).strip()
                if not re.fullmatch(r"127\.0\.0\.1:[0-9]+", binding):
                    raise EnvironmentError("Trial 端口未绑定独立本地地址")
                environment.endpoints[service] = "http://" + binding
            self._endpoints[trial_id] = dict(environment.endpoints)
            self._event(environment, "provision", "PASS")
            return environment
        except Exception:
            self._event(environment, "provision", "INVALID", reason="启动失败；环境句柄保留，可安全清理")
            raise

    def _configuration(self, private, trial_id, owner):
        labels = {f"{LABEL}.owner": owner, f"{LABEL}.trial": trial_id}
        services = {
            "mysql": {"image": "mysql:8.0", "env_file": [str(private / "mysql.env")],
                      "volumes": ["mysql_data:/var/lib/mysql", f"{private}/init.sql:/docker-entrypoint-initdb.d/001-init.sql:ro"],
                      "command": ["--character-set-server=utf8mb4", "--collation-server=utf8mb4_unicode_ci"],
                      "healthcheck": {"test": ["CMD-SHELL", 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysqladmin ping -uroot --silent'],
                                      "interval": "3s", "timeout": "3s", "retries": 40}},
            "redis": {"image": "redis:7.2", "volumes": ["redis_data:/data"],
                      "command": ["redis-server", "--appendonly", "yes"]},
            "minio": {"image": "minio/minio:RELEASE.2024-12-18T13-15-44Z",
                      "env_file": [str(private / "minio.env")], "volumes": ["minio_data:/data"],
                      "command": ["server", "/data"], "ports": ["127.0.0.1::9000"]},
            "rabbitmq": {"image": "rabbitmq:3.13-management", "env_file": [str(private / "rabbitmq.env")],
                         "volumes": ["rabbitmq_data:/var/lib/rabbitmq"], "ports": ["127.0.0.1::15672"]},
            "qdrant": {"image": "qdrant/qdrant:v1.12.1", "volumes": ["qdrant_data:/qdrant/storage"],
                       "ports": ["127.0.0.1::6333"]},
            "workspace": {"image": self.workspace_image, "env_file": [str(private / "workspace.env")],
                          "volumes": [f"{private}/application-benchmark.yml:/config/application-benchmark.yml:ro"],
                          "environment": {"SPRING_PROFILES_ACTIVE": "dev,benchmark",
                                          "SPRING_CONFIG_ADDITIONAL_LOCATION": "file:/config/"},
                          "depends_on": {"mysql": {"condition": "service_healthy"}},
                          "ports": ["127.0.0.1::8098"]},
            "runtime": {"image": self.runtime_image, "env_file": [str(private / "runtime.env")],
                        "volumes": [f"{private}/workdir:/workdir"], "ports": ["127.0.0.1::8000"],
                        "command": ["python", "-m", "hexgent.adapters.web", "--host", "0.0.0.0",
                                    "--port", "8000", "--workdir", "/workdir"]},
        }
        for service in services.values():
            service.update(labels=labels, networks=["trial"], restart="no")
        return {"services": services, "volumes": {f"{s}_data": {"labels": labels} for s in STORES},
                "networks": {"trial": {"labels": labels, "driver": "bridge"}}}

    def _credentials(self, environment):
        self._guard(environment)
        return json.loads((environment.private_root / "credentials.json").read_text())

    def _api(self, environment, method, path, data=None, *, authorized=True):
        self._guard(environment)
        creds = self._credentials(environment)
        headers = {"Authorization": "Bearer " + creds["USER_TOKEN"]} if authorized else {}
        try:
            response = httpx.request(method, environment.endpoints["workspace"] + path,
                json={"data": data} if data is not None else None, headers=headers,
                timeout=15, trust_env=False)
            response.raise_for_status()
            body = response.json()
            if body.get("code") != "0000":
                code = body.get("code")
                safe_code = str(code) if re.fullmatch(r"[A-Za-z0-9_-]{1,32}", str(code)) else "UNKNOWN"
                raise EnvironmentError(f"业务 API 拒绝请求：{safe_code}")
            return body["data"]
        except (httpx.HTTPError, ValueError) as exc:
            raise EnvironmentError("Trial 业务 API 不可用") from exc

    def seed(self, environment, baseline_id):
        self._guard(environment)
        if baseline_id != environment.baseline_id or environment.baseline_sha256 != self.baseline_digest:
            raise EnvironmentError("Baseline 不匹配")
        if environment.seeded or self._sql(environment, "SELECT JSON_OBJECT('count',COUNT(*)) FROM document")[0]["count"]:
            raise EnvironmentError("仅允许在干净 Trial 中 Seed 一次")
        self._event(environment, "seed", "STARTED")
        try:
            credentials = self._credentials(environment)
            account = {"username": "benchmark_user", "password": credentials["USER_PASSWORD"]}
            self._api(environment, "POST", "/api/v1/users/register", account, authorized=False)
            login = self._api(environment, "POST", "/api/v1/users/login", account, authorized=False)
            credentials["USER_TOKEN"] = login["token"]
            dump(environment.private_root / "credentials.json", credentials)
            workspaces = self._api(environment, "GET", "/api/v1/workspaces/my")
            workspace_id = workspaces["workspaces"][0]["workspaceId"]
            root = self._api(environment, "GET", f"/api/v1/workspaces/{workspace_id}/folders/root")
            seeded = []
            for document in self.baseline["documents"]:
                created = self._api(environment, "POST", f"/api/v1/workspaces/{workspace_id}/documents/markdown",
                    {"parentId": root["folderId"], "fileName": document["file_name"], "content": document["content"]})
                seeded.append({"file_name": document["file_name"], "document_id": created["documentId"],
                               "document_code": created["documentCode"]})
            dump(environment.artifact_root / "seed.json", dict(schema_version=1, trial_id=environment.trial_id,
                baseline_id=baseline_id, baseline_sha256=self.baseline_digest, workspace_id=workspace_id,
                user_id=login["userId"], documents=seeded))
            environment.seeded = True
            self._event(environment, "seed", "PASS", document_count=len(seeded))
        except Exception:
            self._event(environment, "seed", "INVALID", reason="业务 Seed 失败")
            raise

    def _health(self, environment):
        checks = {}
        try:
            checks["mysql_schema"] = self._sql(environment,
                "SELECT JSON_OBJECT('tables',COUNT(*)) FROM information_schema.tables WHERE table_schema='hex_workspace'")[0]["tables"] == 19
        except EnvironmentError:
            checks["mysql_schema"] = False
        for service, args in [("redis", ["redis-cli", "ping"]),
                              ("rabbitmq", ["rabbitmq-diagnostics", "-q", "ping"])]:
            try:
                self._exec(environment, service, args); checks[service] = True
            except EnvironmentError:
                checks[service] = False
        for service, path in [("minio", "/minio/health/ready"), ("qdrant", "/readyz"),
                              ("runtime", "/openapi.json")]:
            try:
                checks[service] = httpx.get(environment.endpoints[service] + path, timeout=3,
                                            trust_env=False).status_code == 200
            except httpx.HTTPError:
                checks[service] = False
        try:
            checks["workspace"] = bool(self._api(environment, "GET", "/api/v1/users/me")) if environment.seeded else (
                httpx.get(environment.endpoints["workspace"] + "/api/v1/users/me", timeout=3,
                          trust_env=False).status_code == 401)
        except (EnvironmentError, httpx.HTTPError):
            checks["workspace"] = False
        return checks

    def _object(self, environment, key):
        """签名读取当前 Trial MinIO 对象，不调用上传或删除接口。"""
        credentials = self._credentials(environment)
        endpoint = environment.endpoints["minio"]
        host = urlparse(endpoint).netloc
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        date = timestamp[:8]
        path = "/benchmark/" + quote(key, safe="/-_.~")
        body_hash = sha256(b"").hexdigest()
        headers = {"host": host, "x-amz-content-sha256": body_hash, "x-amz-date": timestamp}
        signed = ";".join(sorted(headers))
        canonical = "GET\n" + path + "\n\n" + "".join(f"{k}:{headers[k]}\n" for k in sorted(headers)) + "\n" + signed + "\n" + body_hash
        scope = f"{date}/us-east-1/s3/aws4_request"
        to_sign = f"AWS4-HMAC-SHA256\n{timestamp}\n{scope}\n{sha256(canonical.encode()).hexdigest()}"
        signing_key = ("AWS4" + credentials["MINIO_PASSWORD"]).encode()
        for value in [date, "us-east-1", "s3", "aws4_request"]:
            signing_key = hmac.new(signing_key, value.encode(), "sha256").digest()
        signature = hmac.new(signing_key, to_sign.encode(), "sha256").hexdigest()
        headers["Authorization"] = f"AWS4-HMAC-SHA256 Credential=benchmark/{scope}, SignedHeaders={signed}, Signature={signature}"
        response = httpx.get(endpoint + path, headers=headers, timeout=5, trust_env=False)
        response.raise_for_status()
        return response.content

    def observe(self, environment):
        self._guard(environment)
        counts = self._sql(environment, "SELECT JSON_OBJECT(" + ",".join(
            f"'{table}',(SELECT COUNT(*) FROM `{table}`)" for table in
            ["user", "workspace", "folder", "document", "document_version", "knowledge_parse_artifact",
             "knowledge_index_job", "knowledge_index_generation"]) + ")")[0]
        documents = self._sql(environment, "SELECT JSON_OBJECT('document_id',d.id,'document_code',d.document_code,"
            "'file_name',d.file_name,'status',d.status,'version_no',d.current_version_no,'version_id',v.id,"
            "'content_hash',v.content_hash,'storage_path',v.storage_path,'source_size',v.content_size,"
            "'parse_ready',(SELECT COUNT(*) FROM knowledge_parse_artifact p WHERE p.document_id=d.id "
            "AND p.version_no=d.current_version_no AND p.source_checksum=v.content_hash AND p.status=1),"
            "'artifact_key',(SELECT MAX(p.artifact_key) FROM knowledge_parse_artifact p WHERE p.document_id=d.id "
            "AND p.version_no=d.current_version_no AND p.source_checksum=v.content_hash AND p.status=1),"
            "'artifact_checksum',(SELECT MAX(p.artifact_checksum) FROM knowledge_parse_artifact p WHERE p.document_id=d.id "
            "AND p.version_no=d.current_version_no AND p.source_checksum=v.content_hash AND p.status=1),"
            "'chunk_artifact_key',g.chunk_artifact_key,'chunk_artifact_checksum',g.chunk_artifact_checksum,"
            "'generation_id',s.active_generation_id,'index_ready',IF(g.status=1 AND g.version_no=d.current_version_no "
            "AND g.index_input_revision=d.index_input_revision,1,0),'chunk_count',g.chunk_count) "
            "FROM document d LEFT JOIN document_version v ON v.document_id=d.id AND v.version_no=d.current_version_no "
            "LEFT JOIN knowledge_index_state s ON s.document_id=d.id "
            "LEFT JOIN knowledge_index_generation g ON g.generation_id=s.active_generation_id ORDER BY d.file_name")
        for document in documents:
            for label, key, expected_hash in [
                    ("source", document["storage_path"], document["content_hash"]),
                    ("parse", document["artifact_key"], document["artifact_checksum"]),
                    ("chunk", document["chunk_artifact_key"], document["chunk_artifact_checksum"])]:
                try:
                    payload = self._object(environment, key) if key else None
                    document[f"{label}_object_valid"] = payload is not None and sha256(payload).hexdigest() == expected_hash
                except (EnvironmentError, httpx.HTTPError):
                    document[f"{label}_object_valid"] = False
            document["qdrant_point_count"] = 0
            if document["generation_id"]:
                try:
                    response = httpx.post(environment.endpoints["qdrant"] + "/collections/benchmark_rag_v1/points/count",
                        json={"exact": True, "filter": {"must": [{"key": "generation_id", "match": {
                            "value": document["generation_id"]}}]}}, timeout=5, trust_env=False)
                    response.raise_for_status()
                    document["qdrant_point_count"] = response.json()["result"]["count"]
                except (httpx.HTTPError, ValueError, KeyError):
                    document["qdrant_point_count"] = None
        pending = self._sql(environment, "SELECT JSON_OBJECT('upload',"
            "(SELECT COUNT(*) FROM document_upload_task WHERE status IN (0,1,3)),"
            "'index',(SELECT COUNT(*) FROM knowledge_index_job WHERE status IN (0,1)),"
            "'upload_outbox',(SELECT COUNT(*) FROM document_upload_outbox_event WHERE status IN (0,2)),"
            "'index_outbox',(SELECT COUNT(*) FROM knowledge_index_outbox_event WHERE status IN (0,2)))")[0]
        try:
            queues = httpx.get(environment.endpoints["rabbitmq"] + "/api/queues",
                auth=("benchmark", self._credentials(environment)["MQ_PASSWORD"]), timeout=5, trust_env=False)
            queues.raise_for_status()
            pending["mq_messages"] = sum(q.get("messages", 0) for q in queues.json())
        except (httpx.HTTPError, ValueError):
            pending["mq_messages"] = -1
        # 跨 Trial 等价摘要排除自增 ID、UUID、时间和对象路径，保留实际业务内容与状态。
        normalized = [{k: d[k] for k in ["file_name", "status", "version_no", "content_hash",
                                        "parse_ready", "index_ready", "chunk_count"]} for d in documents]
        observation = dict(schema_version=1, suite_id=self.suite_id, case_id=self.case_id,
            trial_id=environment.trial_id, baseline_id=environment.baseline_id,
            baseline_sha256=environment.baseline_sha256, observed_at=now(), counts=counts,
            documents=documents, pending_tasks=pending, semantic_sha256=sha256(
                json.dumps(normalized, sort_keys=True).encode()).hexdigest(),
            sources=["Trial mysql: hex_workspace Document/Version/Parse/Generation/Job/Outbox",
                     "Trial MinIO: signed GET + content checksum", "Trial Qdrant: exact generation point count",
                     "Trial RabbitMQ: management queue message count"],
            local_state_paths={"workdir": str(environment.workdir),
                               "memory": str(environment.workdir / ".hexgent/memory"),
                               "sessions": str(environment.workdir / ".hexgent/sessions")})
        dump(environment.artifact_root / "observations/business-state.json", observation)
        self._event(environment, "observe", "PASS")
        return observation

    def readiness(self, environment, *, timeout_seconds=120, poll_seconds=2, require_vector=True,
                  require_seed=True):
        self._guard(environment)
        if not 0 < timeout_seconds <= 600 or not 0 < poll_seconds <= 10:
            raise EnvironmentError("就绪检查必须使用有界超时与轮询间隔")
        self._event(environment, "readiness", "STARTED")
        deadline = time.monotonic() + timeout_seconds
        result = None
        while True:
            checks = self._health(environment)
            observation = None
            if all(checks.values()):
                try:
                    observation = self.observe(environment)
                except EnvironmentError:
                    checks["business_observation"] = False
            docs = observation["documents"] if observation else []
            expected = {d["file_name"] for d in self.baseline["documents"]}
            seeded = environment.seeded and {d["file_name"] for d in docs} == expected
            formal = seeded and all(d["status"] == 2 and d["version_id"] and d["source_object_valid"] for d in docs)
            baseline_hashes = {d["file_name"]: sha256(d["content"].encode()).hexdigest() for d in self.baseline["documents"]}
            seed_equivalent = seeded and all(d["content_hash"] == baseline_hashes[d["file_name"]] for d in docs)
            parsed = formal and all(d["parse_ready"] > 0 and d["parse_object_valid"] for d in docs)
            indexed = parsed and all(d["index_ready"] == 1 and d["chunk_count"] > 0 and d["chunk_object_valid"]
                                    and d["qdrant_point_count"] == d["chunk_count"] for d in docs)
            pending = observation["pending_tasks"] if observation else {}
            settled = observation is not None and not any(pending.values())
            ready = all(checks.values()) and (not require_seed or (
                formal and seed_equivalent and parsed and settled and (indexed or not require_vector)))
            result = dict(schema_version=1, suite_id=self.suite_id, case_id=self.case_id,
                trial_id=environment.trial_id, baseline_id=environment.baseline_id, observed_at=now(),
                status="PASS" if ready else "INVALID", checks=checks, document_version_ready=bool(formal),
                parse_ready=bool(parsed), vector_ready=bool(indexed), initialization_settled=settled,
                baseline_content_equivalent=bool(seed_equivalent),
                require_vector=require_vector, require_seed=require_seed,
                embedding_configured=bool(self.embedding_key), timeout_seconds=timeout_seconds)
            if ready or time.monotonic() >= deadline:
                break
            time.sleep(min(poll_seconds, max(0, deadline - time.monotonic())))
        result["reason"] = "READY" if result["status"] == "PASS" else (
            "EMBEDDING_UNAVAILABLE" if require_vector and not self.embedding_key else "READINESS_TIMEOUT")
        dump(environment.artifact_root / "readiness.json", result)
        dump(environment.artifact_root / "observations/environment.json", result)
        self._event(environment, "readiness", result["status"], reason=result["reason"])
        return result

    def archive(self, environment):
        self._guard(environment)
        # 白名单配置；不输出 env_file 内容、Docker Config.Env 或登录响应。
        resources = self._resources(environment)
        inventory = {kind: [{"id": item.get("Id") or item.get("Name"),
                             "name": item.get("Name"), "labels": item.get("Labels") or item.get("Config", {}).get("Labels")}
                            for item in items] for kind, items in resources.items()}
        snapshot = dict(schema_version=1, suite_id=self.suite_id, case_id=self.case_id,
            trial_id=environment.trial_id, baseline_id=environment.baseline_id,
            baseline_sha256=environment.baseline_sha256, compose_sha256=environment.compose_sha256,
            project=environment.project, endpoints=environment.endpoints, resources=inventory,
            images={"workspace": self.workspace_image, "runtime": self.runtime_image},
            actual_service_images={item["Config"]["Labels"]["com.docker.compose.service"]: item["Image"]
                                   for item in resources["container"]},
            initializer_sha256=sha256((environment.private_root / "init.sql").read_bytes()).hexdigest(),
            lifecycle_code_sha256=self.lifecycle_code_digest,
            credentials="REDACTED", agent_state="独立 workdir，未执行 Agent")
        dump(environment.artifact_root / "environment-config.json", snapshot)
        dump(environment.artifact_root / "baseline.json", self.baseline)
        # 日志可能包含凭证；不原样落盘。只保存退出码与健康状态用于失败诊断。
        dump(environment.artifact_root / "diagnostics.json", {"containers": [
            {"service": item["Config"]["Labels"]["com.docker.compose.service"],
             "status": item["State"]["Status"], "exit_code": item["State"]["ExitCode"],
             "health": item["State"].get("Health", {}).get("Status")}
            for item in resources["container"]]})
        sessions = environment.workdir / ".hexgent/sessions"
        if sessions.is_dir():
            for source in sessions.iterdir():
                if source.is_dir() and not source.is_symlink():
                    shutil.copytree(source, environment.artifact_root / "sessions" / source.name,
                                    dirs_exist_ok=True, ignore=shutil.ignore_patterns("*.lock"))
        self._event(environment, "archive", "PASS")
        return environment.artifact_root

    def destroy(self, environment):
        self._guard(environment, destroyed_ok=True)
        if environment.destroyed:
            self._event(environment, "destroy", "PASS", already_destroyed=True)
            return
        self._event(environment, "destroy", "STARTED")
        try:
            # 必须检查项目全部资源，而不只检查属于本 Trial 的标签子集。
            for kind in ("container", "volume", "network"):
                ids = self.docker.run([kind, "ls", "--quiet", "--filter",
                    f"label=com.docker.compose.project={environment.project}"]).split()
                for item in json.loads(self.docker.run([kind, "inspect", *ids])) if ids else []:
                    labels = item.get("Labels") or item.get("Config", {}).get("Labels") or {}
                    if labels.get(f"{LABEL}.owner") != environment.owner or labels.get(f"{LABEL}.trial") != environment.trial_id:
                        raise EnvironmentError("销毁目标包含非当前 Trial 资源")
            self._compose(environment, "down", "--volumes", "--remove-orphans", timeout=120)
            if any(self._resources(environment).values()):
                raise EnvironmentError("当前 Trial 资源未完全销毁")
            # 保留宿主 workdir 作为可审计路径，仅擦除本次生成的凭证文件。
            for path in [environment.private_root / "credentials.json", *environment.private_root.glob("*.env")]:
                path.unlink(missing_ok=True)
            environment.destroyed = True
            self._event(environment, "destroy", "PASS")
        except Exception:
            self._event(environment, "destroy", "INVALID", reason="安全清理失败，资源需按归属核对")
            raise


WORKSPACE_CONFIG = '''
server:
  port: 8098
spring:
  datasource:
    url: jdbc:mysql://mysql:3306/hex_workspace?useUnicode=true&characterEncoding=utf8&serverTimezone=UTC&useSSL=false&allowPublicKeyRetrieval=true
    username: root
    password: ${DB_PASSWORD}
    hikari:
      minimum-idle: 1
      maximum-pool-size: 5
  rabbitmq:
    host: rabbitmq
    port: 5672
    username: benchmark
    password: ${MQ_PASSWORD}
redis:
  sdk:
    config:
      host: redis
      port: 6379
minio:
  sdk:
    config:
      endpoint: http://minio:9000
      access-key: benchmark
      secret-key: ${MINIO_PASSWORD}
      bucket: benchmark
hexworkspace:
  jwt:
    secret: ${JWT_SECRET}
  hexgent:
    runtime-url: http://runtime:8000
  rag-evaluation:
    token: ${EVALUATION_TOKEN}
  knowledge:
    embedding:
      collection: benchmark_rag_v1
    dispatcher:
      fixed-delay-ms: 1000
    worker:
      retry-delay-seconds: 1
ai:
  embedding:
    dashscope:
      api-key: ${DASHSCOPE_API_KEY:}
  vector-store:
    qdrant:
      endpoint: http://qdrant:6333
      collection: benchmark_segments
'''
