"""Calibration-free placement, routing, and translation for the IBM New study.

Run from the repository root with:
    python -m helpers.controlled_compilation
"""
import json
from pathlib import Path
from statistics import median

import pandas as pd
from qiskit import qasm2
from qiskit.circuit.equivalence_library import SessionEquivalenceLibrary
from qiskit.transpiler import CouplingMap, PassManager, Target
from qiskit.transpiler.passes import (
    BasisTranslator,
    GateDirection,
    SabreLayout,
)

from .comparison import _equivalent, metrics
from .logical_prediction_helpers import CountRoutingSwaps, optimize_suite
from .logical_prediction_v2 import EXPANDED_BACKENDS, _connected_target

ROOT = Path(__file__).resolve().parents[1]
IBM_NEW = ["rz", "sx", "x", "cx"]
SEEDS = tuple(range(5))
LAYOUT_TRIALS = 20
SWAP_TRIALS = 20
LAYOUT_ITERATIONS = 4


def calibration_free_target(target):
    """Preserve instruction availability, including direction, without properties."""
    result = Target(num_qubits=target.num_qubits)
    for name in target.operation_names:
        operation = target.operation_from_name(name)
        properties = None if isinstance(operation, type) else {
            qubits: None for qubits in target[name]
        }
        result.add_instruction(operation, properties, name=name)
    return result


def controlled_compile(
    circuit, source, target, seed, *, layout_trials=LAYOUT_TRIALS,
    swap_trials=SWAP_TRIALS, max_iterations=LAYOUT_ITERATIONS,
):
    """Compile without cancellation, resynthesis, or calibration-based choices."""
    clean = calibration_free_target(target)
    coupling = clean.build_coupling_map()
    if coupling is None:
        coupling = CouplingMap.from_full(clean.num_qubits)
    basis = list(clean.operation_names)
    manager = PassManager([
        SabreLayout(
            coupling, seed=seed, layout_trials=layout_trials,
            swap_trials=swap_trials, max_iterations=max_iterations,
        ),
        CountRoutingSwaps(),
        BasisTranslator(SessionEquivalenceLibrary, basis, target=clean),
        GateDirection(coupling, target=clean),
        BasisTranslator(SessionEquivalenceLibrary, basis, target=clean),
    ])
    physical = manager.run(circuit)
    _equivalent(source, physical, physical=True)
    for instruction in physical.data:
        qubits = tuple(physical.find_bit(qubit).index for qubit in instruction.qubits)
        if not clean.instruction_supported(operation_name=instruction.operation.name, qargs=qubits):
            raise ValueError(f"Unsupported native instruction: {instruction.operation.name} {qubits}")
    counts = physical.count_ops()
    values = {
        **metrics(physical),
        "swaps": manager.property_set["inserted_swaps"],
        "2Q_depth": physical.depth(filter_function=lambda instruction: instruction.operation.num_qubits == 2),
        **{f"gate_{name}": counts.get(name, 0) for name in ("cx", "rz", "sx", "x", "id")},
    }
    return physical, values


def run():
    """Follow the existing Level 1 CX reversal cohort plus the QFT comparator."""
    frame = pd.read_csv(ROOT / "docs/expanded_trials.csv")
    selected = frame[
        (frame.optimization_gate_set == "IBM New")
        & (frame.optimizer == "Qiskit")
        & (frame.compilation_level == 1)
        & frame.backend.isin(["Manila", "Lima", "Jakarta"])
    ]
    pairs = []
    for (name, backend), group in selected.groupby(["circuit", "backend"]):
        logical = group.logical_before_gates - group.logical_after_gates
        compiled = group.compiled_before_gates - group.compiled_after_gates
        if median(logical) > 0 and median(compiled) < 0:
            pairs.append((name, backend))
    cohort = set(pairs)
    for backend in ("Manila", "Lima", "Jakarta"):
        if (selected.circuit.eq("qe_qft_4") & selected.backend.eq(backend)).any():
            pairs.append(("qe_qft_4", backend))
    inputs = {
        name: {"circuit": qasm2.load(
            ROOT / f"benchmarks/ibmnew/{name}.qasm",
            custom_instructions=qasm2.LEGACY_CUSTOM_INSTRUCTIONS,
        )}
        for name in sorted({name for name, _ in pairs})
    }
    optimized, exclusions = optimize_suite(
        inputs, voqc_home=None, output=None, basis=IBM_NEW, optimizers=("Qiskit",),
    )
    if exclusions:
        raise ValueError(f"Logical equivalence failures: {exclusions}")
    devices = {name: EXPANDED_BACKENDS[name]() for name in {backend for _, backend in pairs}}
    patches = {}
    results = []
    for name, backend in sorted(set(pairs)):
        source = inputs[name]["circuit"]
        candidate = optimized[name, "Qiskit"]["circuit"]
        width = source.num_qubits
        key = backend, width
        if key not in patches:
            patches[key] = _connected_target(devices[backend], width)
        target, sites = patches[key]
        trials = []
        for seed in SEEDS:
            _, before = controlled_compile(source, source, target, seed)
            _, after = controlled_compile(candidate, source, target, seed)
            gains = {metric: before[metric] - after[metric] for metric in before}
            trials.append({"seed": seed, "before": before, "after": after, "gains": gains})
        existing = selected[selected.circuit.eq(name) & selected.backend.eq(backend)]
        results.append({
            "circuit": name, "backend": backend, "sites": sites,
            "level1_reversal": (name, backend) in cohort,
            "logical_before": metrics(source), "logical_after": metrics(candidate),
            "gains": {metric: median(trial["gains"][metric] for trial in trials) for metric in trials[0]["gains"]},
            "level1_gate_savings": median(existing.compiled_before_gates - existing.compiled_after_gates),
            "seeds": trials,
        })
        print(f"{name} / {backend}: {results[-1]['gains']['gates']:+g} native gates saved", flush=True)
    payload = {
        "policy": {
            "logical_optimizer": "Qiskit level 3, IBM New, seed 0, exact synthesis",
            "hardware_pipeline": [
                "SabreLayout", "CountRoutingSwaps", "BasisTranslator", "GateDirection", "BasisTranslator",
            ],
            "sabre_layout_trials": LAYOUT_TRIALS,
            "sabre_swap_trials": SWAP_TRIALS,
            "sabre_max_iterations": LAYOUT_ITERATIONS,
            "sabre_common_layout_trials": "Qiskit also adds 3 or 4 topology-specific trials, depending on coupling map",
            "seeds": list(SEEDS),
            "placement": "SabreLayout topology-aware joint placement and routing on a fixed width-matched connected patch",
            "calibrations": "removed from compilation target",
            "cleanup": "none", "equivalence": "checked after every compilation",
        },
        "rows": results,
    }
    path = ROOT / "docs/controlled-compilation.json"
    path.write_text(json.dumps(payload, indent=2, allow_nan=False) + "\n")
    print(f"Saved {len(results)} comparisons to {path}")


if __name__ == "__main__":
    run()
