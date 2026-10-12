# Gate B：保守预算，不用未知价格启动调用

Policy包含max_provider_calls、max_input_tokens_per_call、max_output_tokens_per_call、max_total_input_tokens、max_total_output_tokens、max_estimated_cost、价格来源及confirmed。

每次SDK create之前，在RLock内为输入上界和max_output_tokens全额预留Token、调用数与费用。包含每次Production重试，在途占用额度，所有失败/超时/取消/缺Usage仍保留全部额度，成功也不释放，使上界更加保守。共享Budget跨整个实验，不按Fixture重置。达到任一上限拒绝额外派发。

未确认价格、缺输入Token上界或SDK Retry未关闭时拒绝。费率和上限必须有效，负价拒绝。实时profile一旦发现Usage缺失或超出预留，停止后续请求并保存异常；已经完成的调用不能撤销，也不能声称零费用。

本轮测试使用显式FAKE_TEST_TARIFF_NOT_PROVIDER_PRICE与Production estimator验证控制算法，不能将该estimator当作实际模型输入上界。真实profile仍需Provider确认的窗口/计费约束及可信Tokenizer或经确认的保守边界函数。输出max_output_tokens也需Provider确认它覆盖所有计费输出。

实际本地.env配置：host=www.rightapi.ai，model=gpt-6.1-sol。读取未输出API Key。Production get_client使用OpenAI SDK且未显式关闭自动重试；Eval可with_options(max_retries=0)，不修改Production。

2026-10-11公开只读核查：[Right Code简介](https://docs.rightapi.ai/docs/rc_quick_start/intro)说明请求Token和扣费记录可追溯，但不能据此确定当前账号/路由价格、缓存计价、失败计费和模型上限。公开主页为JS应用，本轮没有登录账单后台、没有模型调用。未采用OpenAI官方价格作为中转站实际价格。

当前金额上界NOT_AVAILABLE，真实成本Gate BLOCKED。最小人工补充：账号路由的input/output/cache最高单价、货币、确认日期与证据引用；失败/超时/取消是否计费；实际最大输入及计费输出；最高额度。未补齐前confirmed=false，实时调用不得发出。
