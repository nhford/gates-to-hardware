# QUESO benchmark setup

The benchmark uses the QUESO mode of GUOQ at revision
`8c4c3a5a6dfc9f7fc375ec16c2180139f0a8cb1a`.
The optimizer runs locally with Java 21. Building requires Maven 3.

From the repository root, build the dependency in the ignored output directory.

```sh
git clone https://github.com/qqq-wisc/guoq.git outputs/logical_prediction/dependencies/guoq
git -C outputs/logical_prediction/dependencies/guoq checkout 8c4c3a5a6dfc9f7fc375ec16c2180139f0a8cb1a
cd outputs/logical_prediction/dependencies/guoq
mvn package -Dmaven.test.skip=true
```

Return to the project root and run the benchmark in the project's Python environment.

```sh
python -m helpers.queso_benchmark
```

QUESO uses total gate count, beam search, temperature 0, a queue limit of
8,000, seed 0, and no resynthesis. Search is bounded to five seconds after
the rule-loading message. JVM startup and loading rules are outside that
budget. The Nam and IBM New bases each use the bundled three-qubit
six-gate concrete rules and three-gate symbolic rules.

Every optimized output must remain in its requested basis and pass the
existing logical equivalence check. Every compiled output passes the
existing physical-layout-aware equivalence check. Those checks compare full
unitaries through eight qubits and three seeded state probes for wider
circuits. Numerical compilation failures are recorded as exclusions.

The adapter caches the best complete verified output with the source,
JAR, and rule hashes. The benchmark runner caches hardware groups and
writes `docs/queso-results.json`, including per-seed metrics, exclusions,
and optimizer policies. Preset baselines come from the same published
circuit/basis/backend/level/seed. Routing Only uses the published controlled
baselines. The chart generator loads completed QUESO results when rebuilding
the chart.

Upstream invocation and gate-set documentation are in the
[GUOQ README](https://github.com/qqq-wisc/guoq#queso).
