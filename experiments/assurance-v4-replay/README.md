# Assurance v4 历史回放

该离线 corpus 冻结 Issue #160 调查时的 26 个 self-hosting abandoned run、#158 的 R1–R8 链和两个
产品变化/Git 冲突控制场景。它只检查 v4 恢复分类是否把执行契约、Tester correction、角色连续性、
资源参数和可重物化 target drift 留在同一 Mission；真实产品变化仍进入 Semantic Revision，Git 冲突
仍保留现场并停止。runner 同时用 runtime 的 trigger category 计算 R1–R8 非语义 pressure，验证第三次
累计 transition 或同 category 第三次会要求 architecture review。

`python3 experiments/assurance-v4-replay/runner.py` 只读取版本化 JSON 并输出机械统计，不读取或修改旧
ledger，不把分类结果冒充真实 Full Driver 交付证据。报告同时冻结任务级第三次 transition 首次停止、
一次授权覆盖同 category 的第 4–6 次、并在第 7 次或新 category 再次停止的固定窗口。
