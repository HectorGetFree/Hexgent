"""冻结 C1 独立验收：仅 FakeModel，真实 Production 请求和持久化路径。"""
import argparse
from contextlib import ExitStack
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
import traceback
import unittest
from unittest.mock import patch

from openai.types.responses import ResponseOutputMessage, ResponseOutputText
from hexgent import agent_loop
from hexgent.agent_engine import AgentEngine, RunRequest
from hexgent.config import HexgentConfig, validate_config
from hexgent.context import ContextManager
from hexgent.context.context_builder import build_context_details
from hexgent.context.context_compaction import COMPACTION_INSTRUCTIONS
from hexgent.events import ToolFinishedEvent
from hexgent.loop.loop_state import LoopState
from hexgent.plan import InMemoryPlanStore, Plan, PlanStep
from hexgent.prompt import SYSTEM_PROMPT, TOOLS
from hexgent.run_control import RunControl
from hexgent.tool.tool_context import ToolExecutionContext
from hexgent.utils.token_estimator import estimate_request_tokens, estimate_value_tokens
from eval.support.agent_contracts import FakeModel, text_response, tool_call

ROOT = Path(__file__).resolve().parents[2]
SUMMARY = "<summary>当前目标：继续核对；重要发现：FACT_C1=ALPHA</summary>"


def plain(value):
    if hasattr(value, "model_dump"):
        return plain(value.model_dump(mode="json"))
    if hasattr(value, "__dataclass_fields__"):
        return plain(asdict(value))
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if isinstance(value, SimpleNamespace):
        return plain(vars(value))
    return value if isinstance(value, (str, int, float, bool, type(None))) else str(value)


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plain(value), ensure_ascii=False, indent=2), encoding="utf-8")


def hashes():
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((ROOT / "hexgent").rglob("*.py"))}


def response(text="完成", calls=None):
    output = calls if calls is not None else [ResponseOutputMessage(
        id="c1-offline", type="message", role="assistant", status="completed",
        content=[ResponseOutputText(type="output_text", text=text, annotations=[])])]
    return text_response(text if calls is None else "", output=output)


class Recorder:
    """模型响应替身；摘要故障只注入 Provider 协议，不替换 Runtime。"""
    def __init__(self, evidence, main=None, summary=None):
        self.evidence = evidence
        self.main = list(main or [response()])
        self.summary = list(summary or [SUMMARY])

    def create(self, **request):
        kind = "COMPACTION" if request["instructions"].startswith(COMPACTION_INSTRUCTIONS) else "MAIN"
        self.evidence["requests"].append(dict(kind=kind, request=plain(request)))
        if kind == "COMPACTION":
            value = self.summary.pop(0) if len(self.summary) > 1 else self.summary[0]
            if isinstance(value, Exception):
                raise value
            if value == "FAILED_EVENT":
                return iter([SimpleNamespace(type="response.failed")])
            if value == "INCOMPLETE_STREAM":
                return iter([])
            value = response(value)
        else:
            if not self.main:
                raise AssertionError("非预注册的额外主模型请求")
            value = self.main.pop(0)
        return iter([SimpleNamespace(type="response.completed", response=value)])


class Gate:
    def __init__(self, output):
        self.output = output
        self.engine = None
        self.e = None

    def check(self, name, condition, details=None):
        self.e["oracles"].append(dict(name=name, pass_=bool(condition), details=plain(details)))
        if not condition:
            raise AssertionError(name)

    def config(self):
        base = HexgentConfig()
        return replace(base, context_extraction=replace(base.context_extraction, enabled=False),
                       context_retrieval=replace(base.context_retrieval, enabled=False),
                       memory_retrieval=replace(base.memory_retrieval, enabled=False))

    def new_engine(self, label="workspace", config=None):
        root = self.directory / label
        root.mkdir(parents=True, exist_ok=True)
        (root / "source.txt").write_text("FACT_C1=ALPHA\nUNIT_C1=ms\n", encoding="utf-8")
        self.engine = AgentEngine(config or self.config(), root)
        return self.engine

    def seed(self, engine, text):
        query = engine.context_manager.append_user_query("较早资料核对")
        engine.session.record_user_query(query)
        messages = response(text).output
        engine.context_manager.append_messages(messages, completes_round=True)
        engine.session.record_context_messages(messages, completes_round=True, source="model_response")

    def freeze_auto(self, engine, query="继续", recent=500, tools=None):
        instructions = build_context_details(engine.tool_context).instructions
        request_tools = TOOLS if tools is None else tools
        base_est = estimate_request_tokens(instructions, [{"role": "user", "content": query}], request_tools)
        engine.config = validate_config(replace(engine.config, context=replace(engine.config.context,
            proactive_threshold_tokens=base_est + 1000, recent_context_tokens=recent,
            reactive_recent_context_tokens=200)))
        self.e.setdefault("frozen_inputs", []).append(dict(config=asdict(engine.config),
            query=query, context=plain(engine.context_manager), base_est=base_est,
            tools=request_tools))
        save(self.directory / "frozen-inputs.json", self.e["frozen_inputs"])

    def run(self, engine, query="继续", main=None, summary=None, pause=False):
        client = Recorder(self.e, main, summary)
        control = RunControl()
        events = []
        with patch("hexgent.loop.agent_turn.get_model", return_value="FAKE_ONLY"):
            for event in engine.run(query, client=SimpleNamespace(responses=client), run_control=control):
                events.append(event)
                if pause and isinstance(event, ToolFinishedEvent):
                    control.pause()
        self.e.setdefault("executions", []).append(dict(query=plain(query), events=plain(events),
            final=plain(events[-1].result), config=asdict(engine.config)))
        self.snapshot(engine, f"state-{len(self.e['executions'])}")
        return events

    def snapshot(self, engine, label):
        save(self.directory / (label + ".json"), dict(context=plain(engine.context_manager),
            transcript=engine.session.store.read_transcript(), session_id=engine.session.store.session_id))
        if engine.session.store.session_id:
            shutil.copytree(engine.session.store.session_dir, self.directory / label,
                            dirs_exist_ok=True)

    def mains(self):
        return [x["request"] for x in self.e["requests"] if x["kind"] == "MAIN"]

    def compactions(self):
        return [x["request"] for x in self.e["requests"] if x["kind"] == "COMPACTION"]

    def reuse(self, names):
        """重跑原测试，透明保存模型实参、预算候选和测试原有工件。"""
        from tests.test_agent_loop_limits import FakeResponses
        from hexgent.loop import agent_turn
        captures = self.e["requests"]
        def wrapper(native):
            def create(obj, **request):
                captures.append(dict(kind="COMPACTION" if request["instructions"].startswith(COMPACTION_INSTRUCTIONS)
                                     else "MAIN", request=plain(request)))
                return native(obj, **request)
            return create
        native_estimate = agent_turn.estimate_request_tokens
        def estimate(instructions, input, tools):
            value = native_estimate(instructions, input, tools)
            self.e.setdefault("budget_candidates", []).append(dict(instructions=instructions,
                input=plain(input), tools=tools, estimated_input_tokens=value))
            return value
        stream = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(patch.object(FakeModel, "create", wrapper(FakeModel.create)))
            stack.enter_context(patch.object(FakeResponses, "create", wrapper(FakeResponses.create)))
            stack.enter_context(patch.object(agent_turn, "estimate_request_tokens", side_effect=estimate))
            stack.enter_context(patch.dict("os.environ", {"PINNED_PHASE2_EVIDENCE_DIR": str(self.directory / "reused"),
                "REQUEST_BUDGET_REPAIR_EVIDENCE_DIR": str(self.directory / "reused")}))
            suite = unittest.defaultTestLoader.loadTestsFromNames(names)
            result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
        (self.directory / "test-output.txt").write_text(stream.getvalue())
        self.e["reused_tests"] = dict(names=names, run=result.testsRun, failures=result.failures,
                                      errors=result.errors, skipped=result.skipped)
        self.check("原测试实际重跑通过", result.wasSuccessful(), self.e["reused_tests"])
        for path in (self.directory / "reused").glob("*.json"):
            data = json.loads(path.read_text())
            self.e.setdefault("original_test_evidence", []).append(dict(path=str(path), data=data))

    def c01(self):
        cfg = self.config()
        cfg = replace(cfg, memory_retrieval=replace(cfg.memory_retrieval, enabled=True))
        engine = self.new_engine(config=cfg)
        engine.tool_context.skill_registry.create("audit", "核对资料", ("read_file", "update_plan"),
                                                   "SKILL_C1：核对 FACT_C1 与 UNIT_C1")
        engine.tool_context.memory_store.upsert("MEM_C1：资料核对使用 ms", "constraint", source="explicit")
        engine.tool_context.plan_store.set_plan(Plan("PLAN_C1：资料核对", "in_progress",
            [PlanStep("核对资料", "正在核对", "in_progress", "step-1")]))
        pin = engine.manage_pinned_constraint("pin", "c1-pin", text="PIN_C1：禁止修改原始资料")
        self.e["sources"] = dict(pin=pin, plan=plain(engine.tool_context.plan_store.get_plan()),
            skill=plain(engine.tool_context.skill_registry.get("audit")), memory=plain(engine.tool_context.memory_store.items))
        # Plan 提醒是既有收尾契约；固定响应序列包含合法完成工具调用。
        update = {"expected_revision": 1, "status": "completed", "steps": [dict(step_id="step-1",
            content="核对资料", process_message="正在核对", status="completed")], "reason": None,
            "blocked_reason": None}
        self.run(engine, "核对资料", main=[response(calls=[tool_call("skill", "activate_skill", '{"name":"audit"}')]),
            response(calls=[tool_call("read", "read_file", '{"path":"source.txt"}')]),
            response(calls=[tool_call("finish-plan", "update_plan", json.dumps(update))]), response()])
        requests = self.mains()
        self.check("实际请求存在激活前后两个阶段", len(requests) >= 3)
        for index, req in enumerate(requests[:3]):
            instr = req["instructions"]
            tags = ["PIN_C1", "PLAN_C1", "<active-skill" if index else "<available-skills", "MEM_C1"]
            positions = [instr.find(tag) for tag in tags]
            self.check("请求来源与顺序-" + str(index), instr.startswith(SYSTEM_PROMPT)
                       and min(positions) >= 0 and positions == sorted(positions), positions)
            self.check("Record关闭", "<relevant-context>" not in instr)
        actual = json.dumps(requests[2]["input"], ensure_ascii=False)
        self.check("真实工具结果交付", 'FACT_C1=ALPHA' in actual and 'UNIT_C1=ms' in actual
                   and 'function_call_output' in actual and 'read' in actual)
        self.check("原始文件未修改", engine.workdir.joinpath("source.txt").read_text() == "FACT_C1=ALPHA\nUNIT_C1=ms\n")

    def c02(self):
        base = "tests.test_pinned_production_context.PinnedProductionContextTests."
        self.reuse([base + name for name in ["test_pinned_limit_rejects_before_any_provider_call",
            "test_full_budget_counts_output_allowance_and_rejects", "test_uncompactable_input_budget_rejects_without_dispatch",
            "test_budget_includes_input_and_tools_and_toml_limit"]])
        self.check("预算候选与实际请求可观测", bool(self.e.get("budget_candidates")) and bool(self.mains()))

    def c03(self):
        engine = self.new_engine()
        self.seed(engine, "首次核对完成")
        self.seed(engine, "第二次核对完成")
        before = plain(engine.context_manager.model_messages())
        self.run(engine)
        self.check("未触发摘要", not self.compactions() and engine.context_manager.compaction_count == 0)
        self.check("实际请求历史原样", self.mains()[0]["input"] == before + [{"role": "user", "content": "继续"}])

    def c04(self):
        self.reuse(["tests.test_agent_loop_limits.AgentLoopLimitsTest.test_context_compaction_is_an_explicit_loop_transition"])
        self.check("先摘要后主请求", [x["kind"] for x in self.e["requests"]] == ["COMPACTION", "MAIN"])
        c, m = self.compactions()[0], self.mains()[0]
        self.check("摘要不带工具，主请求带工具", "tools" not in c and bool(m["tools"]))
        self.check("最终请求含最新问题及摘要", "最新问题" in json.dumps(m["input"], ensure_ascii=False)
                   and "较早会话已经完成初步检查" in json.dumps(m["input"], ensure_ascii=False))

    def c05(self):
        for label, content in [("small", "FACT_C1=ALPHA\nUNIT_C1=ms\n"), ("oversized", "FACT_C1=ALPHA\nUNIT_C1=ms\n" + "资料" * 2500)]:
            engine = self.new_engine(label)
            self.seed(engine, "历史噪声" * 3000)
            engine.workdir.joinpath("source.txt").write_text(content)
            self.run(engine, "读取资料", main=[response(calls=[tool_call(label, "read_file", '{"path":"source.txt"}')]), response()])
            self.freeze_auto(engine)
            start = len(self.e["requests"])
            self.run(engine)
            requests = self.e["requests"][start:]
            c = next(r["request"] for r in requests if r["kind"] == "COMPACTION")
            m = next(r["request"] for r in requests if r["kind"] == "MAIN")
            for phase, req in [("summary", c), ("main", m)]:
                calls = [r["call_id"] for r in req["input"] if r.get("type") == "function_call"]
                outputs = [r["call_id"] for r in req["input"] if r.get("type") == "function_call_output"]
                self.check(label + phase + "原子轮次", calls == outputs, dict(calls=calls, outputs=outputs))
            self.check(label + "最新Query保留", {"role":"user", "content":"继续"} in m["input"])
            self.check(label + "切点行为", (label in [r.get("call_id") for r in m["input"]]) == (label == "small"))

    def c06(self):
        cases = [("timeout", TimeoutError("C1摘要超时")), ("failed", "FAILED_EVENT"),
            ("stream", "INCOMPLETE_STREAM"), ("empty", "<summary></summary>"),
            ("multiple", "<summary>A</summary><summary>B</summary>"),
            ("no_savings", "<summary>FACT_C1=ALPHA；" + "无收益诊断摘要" * 200 + "</summary>")]
        for label, fault in cases + [("retry_success", TimeoutError("首次失败"))]:
            engine = self.new_engine(label)
            if label == "no_savings":
                # 1. 大近期完整轮次原样保留，只有短前缀进入摘要；合法摘要使候选反而变大。
                self.seed(engine, "旧调查" * 50)
                self.seed(engine, "近期原始调查结果" * 150)
                from hexgent.loop.agent_turn import _available_tools_for_state
                tools = _available_tools_for_state(LoopState(engine.context_manager, engine.tool_context))
                self.freeze_auto(engine, recent=2000, tools=tools)
                instructions = build_context_details(engine.tool_context).instructions
                messages = engine.context_manager.model_messages() + [{"role":"user", "content":"继续"}]
                cut = engine.context_manager.round_boundaries[0]
                summary = fault.removeprefix("<summary>").removesuffix("</summary>").strip()
                candidate, _, _ = engine.context_manager.build_compacted_messages(cut, summary)
                candidate_messages = engine.context_manager.materialize_messages(candidate) + [{"role":"user", "content":"继续"}]
                before_tokens = estimate_request_tokens(instructions, messages, tools)
                after_tokens = estimate_request_tokens(instructions, candidate_messages, tools)
                preflight = dict(summary=summary, summary_tokens=estimate_value_tokens(summary),
                    summary_limit=engine.config.context.summary_output_tokens, expected_cut=cut,
                    prefix=messages[:cut], retained_tail=messages[cut:], candidate=candidate_messages,
                    before_tokens=before_tokens, after_tokens=after_tokens,
                    savings_tokens=before_tokens-after_tokens, tools=tools, config=asdict(engine.config))
                self.e["no_savings_fixture"] = plain(preflight)
                save(self.directory / "no-savings-frozen-fixture.json", preflight)
                self.check("no_savings摘要长度合法", 0 < preflight["summary_tokens"] <= preflight["summary_limit"])
                self.check("no_savings自动触发且为soft", engine.config.context.compaction_threshold_tokens <= before_tokens
                           < engine.config.context.blocking_threshold_tokens)
                self.check("no_savings预注册候选确实无收益", after_tokens >= before_tokens)
                self.check("no_savings近期尾部合法保留", estimate_value_tokens(messages[cut:]) <= engine.config.context.recent_context_tokens)
            else:
                self.seed(engine, "历史噪声" * 3000)
                self.freeze_auto(engine)
            before = plain(engine.context_manager.model_messages())
            start = len(self.e["requests"])
            events = self.run(engine, summary=[fault, SUMMARY] if label == "retry_success" else [fault])
            requests = self.e["requests"][start:]
            self.check(label + "有限尝试", sum(r["kind"] == "COMPACTION" for r in requests) == 2)
            self.check(label + "主模型可正常派发", events[-1].result.status == "completed")
            m = next(r["request"] for r in requests if r["kind"] == "MAIN")
            self.check(label + "活动历史转换", engine.context_manager.compaction_count == (1 if label == "retry_success" else 0))
            if label != "retry_success":
                if label == "no_savings":
                    self.verify_no_savings_rejection(events, requests)
                self.check(label + "失败不提交候选", m["input"] == before + [{"role":"user", "content":"继续"}])
                self.check(label + "首轮失败计数", engine.context_manager.consecutive_compaction_failures == 1)
                # 1. 内部 attempt 不等于失败轮数；第二轮仍注入同一预注册故障。
                start2 = len(self.compactions())
                before2 = plain(engine.context_manager.model_messages())
                events2 = self.run(engine, summary=[fault])
                if label == "no_savings":
                    self.verify_no_savings_rejection(events2, self.e["requests"][-3:])
                self.check(label + "第二轮有限尝试", len(self.compactions()) - start2 == engine.config.context.max_compaction_attempts)
                self.check(label + "第二轮失败计数", engine.context_manager.consecutive_compaction_failures == 2)
                self.check(label + "第二轮未提交候选", engine.context_manager.compaction_count == 0
                           and self.mains()[-1]["input"] == before2 + [{"role":"user", "content":"继续"}])
                self.check(label + "第二轮soft可继续", events2[-1].result.status == "completed")
                # 2. 达到两轮失败上限后，第三轮才检查熔断；禁止默认提供合法摘要。
                start3 = len(self.compactions())
                before3 = plain(engine.context_manager.model_messages())
                events3 = self.run(engine, summary=[fault])
                self.check(label + "连续失败熔断", len(self.compactions()) == start3)
                self.check(label + "熔断状态与原历史保留", engine.context_manager.consecutive_compaction_failures == 2
                           and engine.context_manager.compaction_count == 0
                           and self.mains()[-1]["input"] == before3 + [{"role":"user", "content":"继续"}])
                self.check(label + "熔断soft可继续", events3[-1].result.status == "completed")
        engine = self.new_engine("hard")
        start = len(self.mains())
        events = self.run(engine, "巨大新输入" * 40000)
        self.check("硬阈值不可安全压缩零主派发", len(self.mains()) == start
                   and "CONTEXT_BUDGET_EXCEEDED" in events[-1].result.output_text)

    def verify_no_savings_rejection(self, events, requests):
        """拒绝必须来自收益校验，而非长度、格式或 Provider 故障。"""
        messages = [event.message for event in events if hasattr(event, "message")]
        self.check("no_savings拒绝原因必须为压缩收益不足", any("压缩收益不足" in message for message in messages), messages)
        self.check("no_savings未被摘要长度校验抢先拒绝", not any("摘要内容超过配置的摘要 Token 预算" in message for message in messages))
        prefixes = [entry["request"]["input"] for entry in requests if entry["kind"] == "COMPACTION"]
        self.check("no_savings实际摘要范围为预注册短前缀", bool(prefixes) and all(
            prefix == self.e["no_savings_fixture"]["prefix"] for prefix in prefixes))
        mains = [entry["request"] for entry in requests if entry["kind"] == "MAIN"]
        self.check("no_savings实际工具Schema与冻结输入一致", bool(mains) and all(
            request["tools"] == self.e["no_savings_fixture"]["tools"] for request in mains))

    def c07(self):
        engine = self.new_engine()
        self.seed(engine, "历史噪声" * 3000)
        self.freeze_auto(engine, "检查资料然后暂停")
        self.run(engine, "检查资料然后暂停", main=[response(calls=[tool_call("pause", "glob", '{"pattern":"*.txt"}')])], pause=True)
        self.check("安全Continue可达", engine.has_continuable_run)
        before = plain(engine.context_manager.model_messages())
        count = len(engine.context_manager.user_queries)
        sid = engine.session.store.session_id
        restored = AgentEngine.restore(engine.config, engine.workdir, sid)
        self.engine = restored
        self.check("正式恢复活动上下文一致", plain(restored.context_manager.model_messages()) == before)
        start = len(self.mains())
        events = self.run(restored, RunRequest.continue_run())
        self.check("Continue请求与安全快照一致", self.mains()[start]["input"] == before)
        self.check("不新增Query且正常完成", len(restored.context_manager.user_queries) == count
                   and events[-1].result.status == "completed" and restored.session.store.session_id == sid)

    def c08(self):
        self.reuse(["tests.test_pinned_production_context.PinnedProductionContextTests.test_retry_reloads_authority_without_active_run_mutation",
            "tests.test_agent_loop_limits.AgentLoopLimitsTest.test_prompt_too_long_triggers_one_reactive_compaction_retry",
            "tests.test_agent_loop_limits.AgentLoopLimitsTest.test_repeated_prompt_too_long_stops_without_second_reactive_retry"])
        self.check("实际重试请求和预算记录", len(self.mains()) >= 2 and bool(self.e.get("budget_candidates")))

    def c09(self):
        for label, query in [("normal", "继续"), ("over", "x" * 12000)]:
            cfg = self.config()
            if label == "over":
                cfg = replace(cfg, context=replace(cfg.context, context_window_tokens=10000,
                    reserved_output_tokens=100, safety_margin_tokens=100, proactive_threshold_tokens=9000,
                    recent_context_tokens=500, reactive_recent_context_tokens=200))
            cfg = validate_config(cfg)
            root = self.directory / label
            root.mkdir()
            manager = ContextManager()
            manager.append_user_query(query)
            ctx = ToolExecutionContext(InMemoryPlanStore(), root)
            client = Recorder(self.e)
            candidates = []
            def estimate(instructions, input, tools):
                value = estimate_request_tokens(instructions, input, tools)
                candidates.append(dict(instructions=instructions, input=plain(input), tools=tools,
                    estimated_input_tokens=value, output_allowance=8000, config=asdict(cfg)))
                return value
            start = len(self.mains())
            with patch("hexgent.loop.agent_turn.get_model", return_value="FAKE_ONLY"), patch(
                    "hexgent.loop.agent_turn.estimate_request_tokens", side_effect=estimate):
                events = list(agent_loop(LoopState(manager, ctx), config=cfg, client=SimpleNamespace(responses=client)))
            self.e.setdefault("no_session", []).append(dict(label=label, candidates=candidates, events=plain(events),
                provider_dispatch_count=len(self.mains())-start, transcript="NOT_APPLICABLE", checkpoint="NOT_APPLICABLE"))
            self.check(label + "派发标准", len(self.mains()) - start == (1 if label == "normal" else 0))
            self.check(label + "终态标准", events[-1].result.status == ("completed" if label == "normal" else "failed"))
            if label == "over":
                self.check("明确预算拒绝", "CONTEXT_BUDGET_EXCEEDED" in events[-1].result.output_text)

    def c10(self):
        from eval.pinned_constraints.phase3_resume import Acceptance
        Acceptance.output = self.directory
        # 原有 Phase3 Fixture 重新执行，原始证据含 Summary 保留/遗漏和正式 Continue。
        stream = io.StringIO()
        names = ["test_b_requests_compaction_and_lifecycle", "test_c_checkpoint_replace_revoke_continue"]
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(unittest.TestSuite(Acceptance(n) for n in names))
        (self.directory / "test-output.txt").write_text(stream.getvalue())
        self.e["reused_tests"] = dict(run=result.testsRun, failures=result.failures, errors=result.errors)
        self.check("冻结Pinned实际重跑", result.wasSuccessful())
        for path in self.directory.glob("test_*/evidence.json"):
            data = json.loads(path.read_text())
            for group in data["requests"]:
                for req in group["calls"]:
                    self.e["requests"].append(dict(kind="COMPACTION" if req["instructions"].startswith(COMPACTION_INSTRUCTIONS)
                                                   else "MAIN", request=req))

    def execute(self):
        results = []
        stopped = False
        for n in range(1, 11):
            sid = f"C1-{n:02d}"
            if stopped:
                results.append(dict(scenario=sid, status="NOT_TESTED"))
                continue
            self.directory = self.output / sid
            self.directory.mkdir()
            self.engine = None
            self.e = dict(scenario=sid, requests=[], oracles=[], evidence_type="PRODUCTION_DETERMINISTIC_FAKE_MODEL",
                provider_actual_usage="NOT_APPLICABLE", paid_model_calls=0)
            print("START", sid, flush=True)
            try:
                getattr(self, f"c{n:02d}")()
                self.e["status"] = "PASS"
            except Exception as error:
                self.e["status"] = "FAIL" if isinstance(error, AssertionError) else "BLOCKED"
                self.e["error"] = traceback.format_exc()
                stopped = True
            finally:
                if self.engine is not None:
                    self.snapshot(self.engine, "final-state")
                self.e["provider_call_count"] = dict(main=len(self.mains()), compaction=len(self.compactions()))
                save(self.directory / "oracle.json", self.e)
            print(sid, self.e["status"], flush=True)
            results.append(dict(scenario=sid, status=self.e["status"], provider_call_count=self.e["provider_call_count"],
                                evidence=str(self.directory / "oracle.json")))
        return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    out = args.output_dir or ROOT / "eval/context_management/runs" / ("c1-independent-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    out.mkdir(parents=True, exist_ok=False)
    before = hashes()
    inputs = [ROOT / "docs/CONTEXT_MANAGEMENT_C0_AUDIT.md", ROOT / "docs/CONTEXT_MANAGEMENT_C1_PREP.md",
              ROOT / "eval/context_management/runs/c1-09-repair-20261011-local/REPORT.md"]
    save(out / "manifest.json", dict(production_hashes=before, frozen_inputs={str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs},
                                    runner_hash=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), paid_model_calls=0))
    results = Gate(out).execute()
    after = hashes()
    gate = "C1_PASS_READY_FOR_C2_REVIEW" if all(r["status"] == "PASS" for r in results) and before == after else (
        "C1_FAIL" if any(r["status"] == "FAIL" for r in results) else "C1_BLOCKED")
    save(out / "result.json", dict(gate=gate, scenarios=results, production_unchanged=before == after, production_hashes_after=after,
                                  regressions="NOT_RUN_YET", paid_model_calls=0))
    (out / "REPORT.md").write_text("# Context Management C1 独立验收\n\nGate: " + gate +
        "\n\n" + "\n".join(f"- {r['scenario']}: {r['status']}" for r in results) +
        "\n\nProduction 哈希前后一致：" + str(before == after) + "。FakeModel Usage 不作为真实 Provider 成本。\n")
    print(json.dumps(dict(gate=gate, output=str(out)), ensure_ascii=False))


if __name__ == "__main__":
    main()
