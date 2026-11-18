# GuoQ benchmark gate sets

Each row lists the gate instructions used in the OpenQASM files in the corresponding directory under `guoq-benchmarks/`. The gate set is the union across the files in that directory. Individual circuits may use only a subset.

| Circuit type | GuoQ directory | Gate set | Explanation |
| --- | --- | --- | --- |
| IBM new | `ibmnew` | `rz`, `sx`, `x`, `cx` | Arbitrary Z rotations, square-root-of-X and Pauli X for single-qubit operations, and CNOT for entanglement. |
| IBM old | `ibm` | `u1`, `u2`, `u3`, `cx` | The legacy IBM basis. `u1` is a phase gate, `u2` is a two-parameter gate with its middle rotation fixed at π/2, and `u3` is a general three-parameter single-qubit gate up to global phase. `cx` is CNOT. |
| Ion | `ion` | `rx`, `ry`, `rz`, `rxx` | Arbitrary single-qubit rotations about X, Y, and Z, with a two-qubit X⊗X interaction for entanglement. |
| Nam RZ | `nam_rz` | `h`, `x`, `rz`, `cx` | Hadamard, Pauli X, arbitrary Z rotations, and CNOT. |
| Nam T/T† | `nam_t_tdg` | `h`, `x`, `s`, `t`, `tdg`, `cx` | A Clifford+T basis with discrete phase gates. `s` applies a π/2 phase, `t` applies a π/4 phase, and `tdg` applies a −π/4 phase to the `|1⟩` component. |
| Quartz CCX | `benchmarksCCX` | `h`, `x`, `cx`, `ccx` | Hadamard, Pauli X, CNOT, and Toffoli. `ccx` flips its target when both controls are 1. |
| Quartz CCZ | `benchmarksCCZ` | `h`, `x`, `cx`, `ccz` | Hadamard, Pauli X, CNOT, and controlled-controlled-Z. `ccz` multiplies the `|111⟩` component by −1. Hadamards on the target before and after `ccz` turn it into `ccx`. |
| Rigetti | `rigetti` | `rx`, `rz`, `cz` | Single-qubit X and Z rotations, with controlled-Z for entanglement. `cz` multiplies the `|11⟩` component by −1. |

The project’s `benchmarks/ibmnew/`, `benchmarks/nam_rz/`, and `benchmarks/quartz_ccz/` directories contain subsets of the corresponding GuoQ suites. `quartz_ccz` corresponds to GuoQ’s `benchmarksCCZ`.
