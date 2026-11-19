# Agent demo results (2026-11-19)

Produced with [`agent_demo.py`](agent_demo.py): one agent loop, one question, run twice.
**Direct** gives the model the files MCP server's 5 tools, and every result goes into its context (capped at 100k
characters per result, as most MCP clients do). **ToolKernel** gives the model ToolKernel's 7 tools instead, and it
answers by writing Python. The data is the synthetic sample set in `.toolkernel/bench-data`. Each question ran once
per model and mode, with default model settings.

## Questions and correct answers

| # | Question | Data it needs | Correct answer |
|---|---|---|---|
| Q1 | Total EMEA revenue in Q3 2025 | Excel, 24,000 rows | 15,960,536.65 |
| Q2 | Customer segment with the most high-priority tickets in Q3 2025, and how many | JSONL (9,000 tickets) joined to CSV (4,000 customers) | SMB, 201 |
| Q3 | Region with the slowest average resolution time for urgent tickets | Same join, then an average | APAC, 30.46 h |

## Results

| Question | Model | Mode | Answer | Correct | Total tokens | Cost | Time |
|---|---|---|---|---|---:|---:|---:|
| Q1 | deepseek-flash | direct | ≈ $15,960,537 | ✅ | 180,223 | $0.0587 | 133 s |
| Q1 | deepseek-flash | ToolKernel | 15,960,536.65 | ✅ | 33,732 | $0.0029 | 13 s |
| Q1 | gpt-5-mini | direct | "I can't produce that total" | ❌ | 628,263 | $0.0880 | 115 s |
| Q1 | gpt-5-mini | ToolKernel | none: hit the 25-turn limit | ❌ | 77,574 | $0.0067 | 47 s |
| Q2 | deepseek-flash | direct | SMB, 200 | ❌ (off by one) | 1,330,116 | $0.1561 | 373 s |
| Q2 | deepseek-flash | ToolKernel | SMB, 201 (also gave 286 if "urgent" counts as high) | ✅ | 36,703 | $0.0029 | 13 s |
| Q2 | gpt-5-mini | direct | SMB, 612 | ❌ | 924,994 | $0.0973 | 156 s |
| Q2 | gpt-5-mini | ToolKernel | SMB, 201 | ✅ | 73,173 | $0.0067 | 43 s |
| Q3 | deepseek-flash | direct | APAC, 30.46 h | ✅ | 465,304 | $0.0732 | 200 s |
| Q3 | deepseek-flash | ToolKernel | APAC, 30.46 h | ✅ | 62,940 | $0.0039 | 20 s |
| Q3 | gpt-5-mini | direct | EMEA, 38.6 h | ❌ | 201,599 | $0.0320 | 73 s |
| Q3 | gpt-5-mini | ToolKernel | APAC, 30.46 h | ✅ | 32,970 | $0.0054 | 35 s |

## Totals

| | Direct | ToolKernel |
|---|---:|---:|
| Correct | 2 / 6 | 5 / 6 |
| Total tokens | 3,730,499 | 317,092 (11.8× fewer) |
| Cost | $0.505 | $0.029 (about 18× less) |
| Time | 1,050 s | 171 s (about 6× faster) |

## Things worth knowing

- **A direct run failed in a fourth way that isn't in the table.** On the first attempt at Q2, gpt-5-mini's direct run
  grew to 354,362 tokens and the API rejected it: the model's limit is 272,000. The rerun in the table survived and
  answered wrongly.
- **Direct mode can still get it right.** DeepSeek answered Q1 and Q3 correctly, after reading up to 465k tokens and
  taking up to 200 seconds.
- **ToolKernel's failure was going in circles.** On Q1, gpt-5-mini kept calling `read_output` and `search_tools`
  instead of loading the table, and ran out of turns. The earlier benchmark found the same weakness with this model.
- **This is a small sample:** 3 questions, 2 models, 1 run each, synthetic data. Treat it as an illustration, not a
  measurement. Run it yourself with `python examples/agent_demo.py --model <model> "<question>"`.
