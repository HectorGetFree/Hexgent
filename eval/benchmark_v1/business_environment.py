"""复用 B1-B，扩展固定 Case Seed 与正常 Provider 配置，不改 Runtime。"""

from hashlib import sha256
import json
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from .business_cases import BUSINESS_TOOLS
from .environment import EnvironmentError, TrialEnvironmentManager, dump


def container_proxy(value):
    """仅映射已有宿主代理；容器内 127.0.0.1 不是宿主代理。"""
    parsed=urlparse(value)
    if parsed.scheme not in {'http','https'} or not parsed.hostname:
        raise EnvironmentError('不支持的 Provider 出站代理')
    if parsed.hostname in {'localhost','127.0.0.1','::1'}:
        credentials=parsed.netloc.rsplit('@',1)[0]+'@' if '@' in parsed.netloc else ''
        netloc=credentials+'host.docker.internal'+(':'+str(parsed.port) if parsed.port else '')
        return urlunparse(parsed._replace(netloc=netloc))
    return value


class BusinessCaseEnvironment(TrialEnvironmentManager):
    def __init__(self, *args, contract, baseline, provider, **kwargs):
        super().__init__(*args, suite_id=contract.suite_id, case_id=contract.case_id, **kwargs)
        self.contract, self.provider = contract, provider
        if not all(provider.get(key) for key in ["OPENAI_API_KEY", "OPENAI_BASE_URL", "MODEL_ID"]):
            raise EnvironmentError("真实 Agent Provider 配置缺失")
        if any("\n" in str(v) or "\r" in str(v) for v in provider.values()):
            raise EnvironmentError("Provider 配置包含非法换行")
        self.baseline = baseline
        self.baseline_digest = sha256(json.dumps(baseline, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def _sql(self, environment, statement):
        # MySQL 镜像内 CLI 默认字符集会丢失中文输出；业务 JDBC 存储仍为 utf8mb4。
        if not statement.startswith("SELECT ") or ";" in statement:
            raise EnvironmentError("观察 SQL 仅允许单条 SELECT")
        raw=self._exec(environment,"mysql",["sh","-c",
            'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -uroot --default-character-set=utf8mb4 --batch --raw --skip-column-names hex_workspace'],
            input=statement+"\n")
        return [json.loads(line) for line in raw.splitlines() if line]

    def _configuration(self, private, trial_id, owner):
        config = super()._configuration(private, trial_id, owner)
        # 不复制宿主 .env，仅注入已检查的三项配置；密钥不进入 Artifact。
        with (private / "runtime.env").open("a", encoding="utf-8") as stream:
            for key in ["OPENAI_API_KEY", "OPENAI_BASE_URL", "MODEL_ID"]:
                stream.write(f"{key}={self.provider[key]}\n")
            for key in ['HTTP_PROXY','HTTPS_PROXY']:
                if self.provider.get(key): stream.write(f'{key}={container_proxy(self.provider[key])}\n')
            stream.write('NO_PROXY=localhost,127.0.0.1,workspace,runtime,mysql,minio,qdrant,rabbitmq,redis\n')
        # 使用既有权限请求接口按合同决定；不打开 auto_approve 或修改权限管线。
        config["services"]["workspace"]["environment"]["HEXWORKSPACE_AGENT_PERMISSION_MODE"] = "default"
        budget = self.contract.execution_budget
        (private / "workdir/hexgent.toml").write_text(f'''[loop]
enforce_run_limits = true
max_model_rounds = 40
max_tool_calls = {budget.max_tool_calls}
max_total_tokens = {budget.max_provider_tokens}
max_elapsed_seconds = {budget.max_wall_time_seconds}
max_output_tokens_per_round = 4096
[context_extraction]
enabled = false
[context_retrieval]
enabled = false
[tool_parallel]
enabled = false
max_workers = 4
''')
        return config

    def start_stage(self, environment, conversation_id, query):
        return self._api(environment, "POST", f"/api/v1/conversations/{conversation_id}/runs",
            {"instruction": query, "resourceScope": "{}", "allowedTools": json.dumps(
                [tool for tool in self.contract.allowed_tools if tool in BUSINESS_TOOLS])})

    def seed(self,environment,baseline_id):
        super().seed(environment,baseline_id)
        expected=self.baseline.get('target_folder_id')
        if expected is not None:
            seed=json.loads((environment.artifact_root/'seed.json').read_text())
            folder=self._api(environment,'GET',f"/api/v1/workspaces/{seed['workspace_id']}/folders/root")
            dump(environment.artifact_root/'public-business-context.json',folder)
            if folder['folderId']!=expected:
                raise EnvironmentError('公开目标文件夹身份与固定Baseline不一致')

    def observe_versions(self, environment):
        """从不可变版本和正式对象读取正文，绝不以 Tool 的成功响应代替业务结果。"""
        versions = self._sql(environment, "SELECT JSON_OBJECT('document_id',v.document_id,'version_id',v.id,"
            "'version_no',v.version_no,'content_hash',v.content_hash,'storage_path',v.storage_path) "
            "FROM document_version v ORDER BY v.document_id,v.version_no")
        for version in versions:
            try:
                raw = self._object(environment, version["storage_path"])
                version["object_checksum_valid"] = sha256(raw).hexdigest() == version["content_hash"]
                version["content"] = raw.decode("utf-8")
            except Exception:
                version["object_checksum_valid"] = False
                version["content"] = None
        mutations = self._sql(environment, "SELECT JSON_OBJECT('run_id',run_id,'call_id',call_id,"
            "'capability',capability,'document_id',document_id,'previous_version_id',previous_version_id,"
            "'version_id',version_id,'status',status) FROM capability_mutation_record ORDER BY id")
        return {"versions": versions, "mutations": mutations}
