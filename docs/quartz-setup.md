# Optional fidelity and Quartz setup

Activate the project's notebook environment before installing Aer.

```sh
python -m pip install qiskit-aer==0.18.2
```

The expanded benchmark uses the upstream Quartz Python bindings from commit
[`2a5892efd6dab9ce763510abb131a2615e24205c`](https://github.com/quantum-compiler/quartz/commit/2a5892efd6dab9ce763510abb131a2615e24205c)
and its `eccset/Nam_5_3_complete_ECC_set.json`. Clone Quartz into
`outputs/logical_prediction/dependencies/quartz`, check out that commit, and
build its runtime and Python extension using the notebook's Python interpreter.
The local build used CMake, Clang, pybind11 3.1.0, Cython 3.3.1, and setuptools 84.2.1.

On the project's Mac, the build needed the following adjustments in the external checkout:

- Remove the macOS `-Xclang -fopenmp` and `target_link_libraries(quartz_runtime LINK_PUBLIC ${OPENMP_LIB})` lines from `CMakeLists.txt`. The benchmark uses the serial rewrite API.
- Remove `add_compile_definitions(USE_RATIONAL)` from `CMakeLists.txt` so the parser preserves decimal rotation angles. In `src/quartz/tasograph/tasograph.h`, change the default `param_precision` from 6 to 16 so QASM export preserves them too.
- In `python/setup.py`, change the `"../src/quartz/"` include entry to `str(Path("../src/quartz").resolve())`. Quartz derives its repository location from a header path at import time.
- In `python/quartz/_cython/core.pyx`, move the top-level `dgl` and `torch` imports inside `to_dgl_graph`. The benchmark uses neither graph-learning dependency.

With those adjustments, run the following from the Quartz checkout with the project environment active.

```sh
python -m pip install pybind11==3.1.0 Cython==3.3.1 setuptools==84.2.1
QUARTZ_ROOT="$PWD"
cmake -S . -B build -DPython_EXECUTABLE="$(command -v python)"
cmake --build build --target quartz_runtime -j 8
cd python
CXXFLAGS="-I$QUARTZ_ROOT/src" \
LDFLAGS="-L$QUARTZ_ROOT/build -Wl,-rpath,$QUARTZ_ROOT/build" \
python -m pip install --no-build-isolation .
```

Keep the checkout and runtime in place. Confirm that `import quartz` works
from the project directory before enabling the expanded benchmark. The runner
verifies each returned circuit against the original unitary. An output that
fails the check is listed in `expanded_policy.json` and omitted from the chart.
