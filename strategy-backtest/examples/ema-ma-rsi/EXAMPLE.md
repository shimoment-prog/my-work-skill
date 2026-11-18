# EMA / MA / RSI 关键 K 示例

这里保留此前专用策略的完整实现，便于明确指定该策略时复算和回归验证。规则见 `references/strategy-rules.md`，运行参数见 `references/running.md`。

本目录的 `scripts/run_review.py` 只执行该示例。其它策略按主 skill 的通用工作流编写适配器，不能只修改本例的策略名称后继续使用其进出场规则。

示例包含自己的图表资产，旧版回测仍可重现；通用图表由主目录的 `scripts/render_review.py` 根据统一结果契约生成。
