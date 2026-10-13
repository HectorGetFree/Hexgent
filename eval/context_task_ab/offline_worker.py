"""独立进程离线分支；没有真实Provider构造或付费入口。"""
import argparse
from dataclasses import replace
import json
from pathlib import Path

from hexgent.agent_engine import AgentEngine, RunRequest
from .harness import config, profile
from .test_gate import Model
from eval.context_management.c1_independent import response, plain

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--work',required=True);parser.add_argument('--session',required=True)
    parser.add_argument('--variant',choices=['ON','OFF'],required=True)
    parser.add_argument('--scaled-threshold',type=int)
    parser.add_argument('--output',required=True)
    args=parser.parse_args();root=Path(args.work);cfg=config(root)
    if args.scaled_threshold:
        cfg=replace(cfg,context=replace(cfg.context,proactive_threshold_tokens=args.scaled_threshold,
            recent_context_tokens=100,reactive_recent_context_tokens=100,
            min_compaction_savings_tokens=1,min_compaction_savings_ratio=.001))
    transcript=root/'.hexgent/sessions'/args.session/'transcript.jsonl'
    pre_restore_transcript=[json.loads(line) for line in transcript.read_text().splitlines()]
    engine=AgentEngine.restore(cfg,root,args.session)
    before=engine.session.store.read_transcript();client=Model([response('离线Continue链路完成；不是采购任务成功证据')])
    with profile(args.variant):events=list(engine.run(RunRequest.continue_run(),client=client,user_confirmation=lambda *a:'allow'))
    result=dict(variant=args.variant,status=events[-1].result.status,requests=client.requests,
        events=plain(events),pre_restore_transcript=pre_restore_transcript,before_transcript=before,after_transcript=engine.session.store.read_transcript(),
        session_id=args.session,compaction_count=engine.context_manager.compaction_count,
        context=plain(engine.context_manager.model_messages()),evidence_type='DETERMINISTIC_MECHANISM_EVIDENCE',
        scaled_threshold=args.scaled_threshold,provider_usage='NOT_APPLICABLE_FAKE_MODEL')
    target=Path(args.output)
    with target.open('x') as f:json.dump(result,f,ensure_ascii=False,indent=2)

if __name__=='__main__':main()
