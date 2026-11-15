# Optional Aer and Quartz setup

Activate the project's notebook environment before installing Aer.

```sh
python -m pip install qiskit-aer==0.18.2
```

The expanded benchmark uses the native Quartz runtime from commit
[`2a5892efd6dab9ce763510abb131a2615e24205c`](https://github.com/quantum-compiler/quartz/commit/2a5892efd6dab9ce763510abb131a2615e24205c)
and its `eccset/Nam_5_3_complete_ECC_set.json`. Clone Quartz into
`outputs/logical_prediction/dependencies/quartz` and check out that commit.

Remove `add_compile_definitions(USE_RATIONAL)` from Quartz's `CMakeLists.txt`
so the runtime preserves decimal rotation angles. On macOS without OpenMP,
also remove the `-Xclang -fopenmp` flag and the line linking
`quartz_runtime` against `OPENMP_LIB`. The notebook compiles its small native
driver on first use. It needs a C++17 compiler and the runtime built as follows
from the Quartz checkout with the notebook environment active.

```sh
python -m pip install pybind11==3.1.0
cmake -S . -B build-float -DPython_EXECUTABLE="$(command -v python)"
cmake --build build-float --target quartz_runtime -j 8
```

Quartz uses its Nam Toffoli-flip preprocessor on five equivalent CCZ
inputs from GuoQ, copied into `benchmarks/quartz_ccz`. The driver compensates
for the pinned Quartz runtime's internal half-angle convention when importing
the CCZ decomposition rules. It merges RZ rotations in all circuits, including
those supplied directly in Nam form. It then runs the native gate-count search
with a 60-second search budget and allows equal-cost candidates.

The notebook checks each CCZ input and both Quartz output stages against the
original Nam circuit. Verified QASM outputs are saved under
`outputs/logical_prediction/quartz_cache/`. A change to the input, driver,
runtime, equivalence set, or search budget selects a new cache entry. Backend
compilation still runs when you recompute the expanded benchmark.

### Rigetti basis

Enable `INCLUDE_RIGETTI` in the notebook to add Quartz runs in the Rigetti
basis alongside the Nam runs. Both start from the same GuoQ circuit. The
Rigetti path translates it to `rx`, `rz`, and `cz` with Qiskit at level 0,
then merges RZ rotations and searches using the bundled
`experiment/ecc_set/rigetti_3_3_5_ecc.json` rules. The adapter translates
the legacy ECC format for the pinned runtime. In those rules, X rotations
are restricted to π and ±π/2. Exported circuits use standard QASM gates
and undergo the same equivalence checks as Nam outputs.

The comparison holds hardware compilation settings and search budgets
fixed within each backend group. Logical gate-count changes are measured
from the original Nam input, so they include basis translation. Use the
compiled metrics to compare the two paths on the same hardware target.

The Rigetti comparison also runs hardware-unaware Qiskit level 3 directly
in the Rigetti basis. VOQC uses its verified Nam passes, then translates
the output to Rigetti at Qiskit level 0 before hardware compilation.
