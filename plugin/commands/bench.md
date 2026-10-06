---
description: Summarise Fusion's latest benchmark run (cost, speed and quality against single models)
argument-hint: [run id, or leave empty for the latest]
allowed-tools: Bash(fusion bench list *) Bash(fusion bench report *) Bash(uvx * fusion bench list *) Bash(uvx * fusion bench report *)
---
Summarise a Fusion benchmark run: $ARGUMENTS

Benchmark results are files in the `bench-results/` directory of the project where the study was
run. Run the commands there, using `fusion` if it is installed and otherwise
`uvx --python '>=3.11' --from git+https://github.com/alexandre0sheva/fusion-code-orchestrator fusion`.

1. `fusion bench list --limit 5` lists runs, newest first. If it lists none, say that no benchmark
   has been run here and point to `docs/BENCHMARK_RESULTS.md` in the Fusion repository for the
   published study. Stop.
2. `fusion bench report RUN_ID` (the id above, or the latest) prints the statistics and verdicts.

Report, per arm, quality with its interval, cost per solved task and median seconds, then the
verdicts (cheaper, faster, not worse, better: yes, no or inconclusive) with the reason the report
gives. Quote the report's own verdicts; do not upgrade "inconclusive". Mention the number of tasks
and repeats, since small studies have wide intervals. This command only reads results; it never
starts a study, which costs money.
