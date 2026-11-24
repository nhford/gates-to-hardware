"""Aer validation and matched-seed superconducting benchmarks."""

import hashlib
import json
import math
from collections import defaultdict
from statistics import median

import pandas as pd
from qiskit.quantum_info import Operator, average_gate_fidelity
from qiskit.transpiler import Target
from qiskit_ibm_runtime.fake_provider import FakeJakartaV2, FakeMarrakesh, FakeSherbrooke

from .logical_prediction_helpers import (
    BACKENDS, compile_and_measure, flatten_point, load_suite, metrics, native_translation_metrics, optimize_suite,
)
from .comparison import BASIS
from .quartz_nam import RIGETTI_BASIS, quartz_optimizer


IBM_NEW_BASIS = ("rz", "sx", "x", "cx")

EXPANDED_BACKENDS = {**BACKENDS, "Jakarta": FakeJakartaV2, "Sherbrooke": FakeSherbrooke,
                     "Marrakesh": FakeMarrakesh}
EXTRA_CIRCUITS = (
    "adder_4", "qaoa_n4_p4", "vqe_4", "vqe_uccsd_n4",
    "heisenberg_step_5_4", "heisenberg_step_20_4", "heisenberg_step_50_4",
    "TFIM_step_10_4", "TFIM_step_50_4", "TFIM_step_100_4", "XY_step_100_4",
    "tof_4", "barenco_tof_4", "hwb6", "qaoa_n6_p4", "qaoa_n8_p4",
    "vqe_nativegates_ibm_tket_8", "qgan_nativegates_ibm_tket_8",
    "qaoa_nativegates_ibm_tket_8", "XY_step_100_8", "TFIM_step_100_8",
)

MARRAKESH_CIRCUITS = (
    "qe_qft_4",
    "tof_5", "barenco_tof_5", "qaoa_n10_p4", "qaoa_n14_p4", "qft_16",
    "TFIM_step_100_16", "heisenberg_step_20_4", "heisenberg_step_50_4",
    "TFIM_step_100_4", "TFIM_step_100_8", "heisenberg_step_35_4",
    "heisenberg_step_45_4",
)


def _aer_simulator(backend):
    from qiskit_aer import AerSimulator
    from qiskit_aer.noise import NoiseModel

    return AerSimulator(method="superop", noise_model=NoiseModel.from_backend(backend),
                        max_parallel_threads=1)


def aer_average_fidelity(compiled_circuit, backend, *, simulator=None):
    """Score the simulated operation against its noiseless physical-order unitary."""
    if compiled_circuit.num_qubits > 5:
        raise ValueError("Full-channel Aer validation is limited to five qubits.")
    if simulator is None:
        simulator = _aer_simulator(backend)
    ideal = Operator(compiled_circuit)
    noisy_circuit = compiled_circuit.copy()
    noisy_circuit.save_superop()
    noisy = simulator.run(noisy_circuit).result().data(0)["superop"]
    # Aer constructs the channel from noise operations. Skip a redundant Choi eigendecomposition.
    return float(average_gate_fidelity(noisy, ideal, require_cp=False))


def compare_aer_fidelity(points, compiled_circuits, *, output):
    simulators = {}
    scores = {}
    rows = []
    for point in points:
        name, backend_name, optimizer = (point[key] for key in ("circuit", "backend", "optimizer"))
        if backend_name not in simulators:
            backend = BACKENDS[backend_name]()
            simulators[backend_name] = (backend, _aer_simulator(backend))
        backend, simulator = simulators[backend_name]
        for label in ("Baseline", optimizer):
            key = (name, backend_name, label)
            if key not in scores:
                scores[key] = aer_average_fidelity(compiled_circuits[key], backend, simulator=simulator)
        before = scores[name, backend_name, "Baseline"]
        after = scores[name, backend_name, optimizer]
        proxy_before = point["compiled_before"]["fidelity"]
        proxy_after = point["compiled_after"]["fidelity"]
        rows.append({
            "circuit": name, "backend": backend_name, "optimizer": optimizer,
            "aer_before": before, "aer_after": after, "aer_gain_pp": 100 * (after - before),
            "proxy_before": proxy_before, "proxy_after": proxy_after,
            "proxy_gain_pp": (100 * (proxy_after - proxy_before)
                              if proxy_before is not None and proxy_after is not None else None),
        })
    fidelity = pd.DataFrame(rows)
    rankings = []
    for (name, backend), group in fidelity.groupby(["circuit", "backend"]):
        if len(group) < 2 or group["proxy_gain_pp"].isna().any():
            continue
        def winners(metric):
            return ", ".join(sorted(group.loc[group[metric].max() - group[metric] <= 1e-6, "optimizer"]))
        proxy, aer = winners("proxy_gain_pp"), winners("aer_gain_pp")
        rankings.append({"circuit": name, "backend": backend, "proxy_winner": proxy,
                         "aer_winner": aer, "agree": proxy == aer})
    rankings = pd.DataFrame(rankings, columns=["circuit", "backend", "proxy_winner", "aer_winner", "agree"])
    output.mkdir(parents=True, exist_ok=True)
    fidelity.to_csv(output / "aer_fidelity.csv", index=False)
    rankings.to_csv(output / "aer_rankings.csv", index=False)
    return fidelity, rankings


def _connected_target(backend, width):
    """Restrict the snapshot to a deterministic connected patch of exactly width wires."""
    target = backend.target
    if width == target.num_qubits:
        return target, list(range(width))
    neighbors = defaultdict(set)
    for a, b in backend.coupling_map.get_edges():
        neighbors[a].add(b)
        neighbors[b].add(a)
    sites = [0]
    for site in sites:
        if len(sites) >= width:
            break
        new_sites = sorted(neighbors[site] - set(sites))
        sites.extend(new_sites[:width - len(sites)])
    if len(sites) != width:
        raise ValueError(f"{backend.name}: no connected {width}-qubit patch from site 0")
    sites.sort()
    local_index = {site: index for index, site in enumerate(sites)}
    patch = Target(num_qubits=width, dt=target.dt, description=f"{backend.name} sites {sites}",
                   granularity=target.granularity, min_length=target.min_length,
                   pulse_alignment=target.pulse_alignment, acquire_alignment=target.acquire_alignment,
                   qubit_properties=[target.qubit_properties[site] for site in sites]
                   if target.qubit_properties else None)
    for name in ("h", "x", "rz", "sx", "id", "cx", "ecr", "cz"):
        if name not in target:
            continue
        entries = {
            tuple(local_index[site] for site in qubits): properties
            for qubits, properties in target[name].items()
            if qubits is not None and all(site in local_index for site in qubits)
        }
        if entries:
            patch.add_instruction(target.operation_from_name(name), entries, name=name)
    return patch, sites


def _summarize_trials(trials):
    groups = defaultdict(list)
    for trial in trials:
        key = (trial["circuit"], trial["backend"], trial["optimizer"],
               trial.get("optimization_gate_set", "Nam RZ"), trial.get("compilation_level", 1))
        groups[key].append(trial)
    summaries = []
    for group in groups.values():
        gains, spread = {}, {}
        for metric in group[0]["compiled_before"]:
            paired = []
            for row in group:
                before, after = row["compiled_before"][metric], row["compiled_after"][metric]
                if before is not None and after is not None:
                    paired.append(100 * (after - before) if metric == "fidelity" else before - after)
            if len(paired) == len(group):
                gains[metric] = median(paired)
                spread[metric] = [min(paired), max(paired)]
        t99_changes = []
        for row in group:
            before = _time_to_99(row["compiled_before"])
            after = _time_to_99(row["compiled_after"])
            if before is None or after is None or before <= 0:
                break
            t99_changes.append(100 * (after - before) / before)
        if len(t99_changes) == len(group):
            gains["t99_relative_pct"] = median(t99_changes)
            spread["t99_relative_pct"] = [min(t99_changes), max(t99_changes)]
        summaries.append({**group[0], "seeds": [row["seed"] for row in group],
                          "gains": gains, "spread": spread})
    return summaries


def _time_to_99(metrics):
    """Estimated runtime for at least 99% chance of one successful run."""
    duration = metrics.get("duration_ns")
    fidelity = metrics.get("fidelity")
    if (duration is None or fidelity is None or duration <= 0
            or not 0 < fidelity <= 1):
        return None
    attempts = 1 if fidelity == 1 else math.ceil(math.log(0.01) / math.log1p(-fidelity))
    return duration * attempts


def run_expanded_benchmark(circuits, backends, routing_seeds, *, suite, voqc_home,
                           output, quartz_ecc=None, quartz_seconds=60, common_only=False, compilation_levels=(0, 1, 2, 3),
                           quartz_rigetti_ecc=None, rigetti_only=False, include_rigetti=False, ibm_new_only=False):
    """Compile matched seeds and report medians of paired gains for each comparison."""
    compilation_levels = tuple(compilation_levels)
    if not compilation_levels or len(set(compilation_levels)) != len(compilation_levels) or set(compilation_levels) - {0, 1, 2, 3}:
        raise ValueError("Select distinct compilation levels from 0 through 3.")
    routing_seeds = tuple(routing_seeds)
    if not routing_seeds or len(routing_seeds) != len(set(routing_seeds)):
        raise ValueError("Supply at least one routing seed and no duplicates.")
    devices = {name: EXPANDED_BACKENDS[name]() for name in backends}
    if not devices:
        raise ValueError("Select at least one backend.")
    if ibm_new_only:
        suite = suite.with_name("ibmnew")
        missing = [name for name in circuits if not (suite / f"{name}.qasm").is_file()]
        if missing:
            print("No IBM New QASM:", ", ".join(missing))
        circuits = [name for name in circuits if name not in missing]
    else:
        missing = []
    inputs = load_suite(circuits, suite, min_width=4, max_width=16,
                        basis=IBM_NEW_BASIS if ibm_new_only else BASIS)
    if common_only:
        width_limit = min(device.num_qubits for device in devices.values())
        inputs = {name: item for name, item in inputs.items()
                  if item["circuit"].num_qubits <= width_limit}
    if not inputs:
        raise ValueError("No eligible inputs.")
    quartz_optimize = quartz_optimizer(quartz_ecc, quartz_seconds, output.parent / "quartz_cache") if quartz_ecc is not None else None
    output.mkdir(parents=True, exist_ok=True)
    if ibm_new_only:
        optimized, exclusions = optimize_suite(inputs, voqc_home=voqc_home, output=output,
                                               basis=IBM_NEW_BASIS, optimizers=("Qiskit",))
    else:
        optimized, exclusions = ({}, []) if rigetti_only else optimize_suite(
            inputs, voqc_home=voqc_home, output=output, quartz_optimize=quartz_optimize)
    candidates_by_basis = {"IBM New" if ibm_new_only else "Nam RZ": optimized}
    if include_rigetti or rigetti_only or quartz_rigetti_ecc is not None:
        rigetti_optimize = None
        if quartz_rigetti_ecc is not None:
            rigetti_optimize = quartz_optimizer(
                quartz_rigetti_ecc, quartz_seconds, output.parent / "quartz_cache", rigetti=True)
        candidates_by_basis["Rigetti"], rigetti_exclusions = optimize_suite(
            inputs, voqc_home=voqc_home, output=output, quartz_optimize=rigetti_optimize,
            basis=RIGETTI_BASIS)
        exclusions.extend(f"{item} / Rigetti" for item in rigetti_exclusions)
    compilation_exclusions = []
    patches = {}
    trials = []
    for name, item in inputs.items():
        source = item["circuit"]
        logical_before = metrics(source)
        candidates = {
            (basis, label): run["circuit"]
            for basis, optimized in candidates_by_basis.items()
            for (key, label), run in optimized.items() if key == name
        }
        if not candidates:
            continue
        logical_after = {key: metrics(circuit) for key, circuit in candidates.items()}
        for backend_name, backend in devices.items():
            width = source.num_qubits
            if width > backend.num_qubits:
                continue
            if (backend_name, width) not in patches:
                patches[backend_name, width] = _connected_target(backend, width)
            target, sites = patches[backend_name, width]
            native_before = native_translation_metrics(source, target)
            native_after = {key: native_translation_metrics(candidate, target)
                            for key, candidate in candidates.items()}
            for level in compilation_levels:
                baseline = {}
                for seed in routing_seeds:
                    try:
                        _, baseline[seed] = compile_and_measure(
                            source, source, target, seed, optimization_level=level)
                    except ValueError as error:
                        if "equivalence at tolerance" not in str(error):
                            raise
                        print(f"Excluded baseline: {name} / {backend_name} / level {level} / seed {seed}")
                        break
                if len(baseline) != len(routing_seeds):
                    compilation_exclusions.append(dict(circuit=name, backend=backend_name,
                                                       compilation_level=level, seed=seed, optimizer="Baseline"))
                    continue
                for (basis, optimizer), candidate in candidates.items():
                    paired = []
                    for seed in routing_seeds:
                        try:
                            _, after = compile_and_measure(
                                candidate, source, target, seed, optimization_level=level)
                        except ValueError as error:
                            if "equivalence at tolerance" not in str(error):
                                raise
                            compilation_exclusions.append(dict(circuit=name, backend=backend_name,
                                compilation_level=level, seed=seed, optimizer=optimizer, optimization_gate_set=basis))
                            print(f"Excluded candidate: {name} / {backend_name} / {optimizer} / level {level} / seed {seed}")
                            break
                        paired.append(dict(circuit=name, backend=backend_name, optimizer=optimizer,
                                           seed=seed, compilation_level=level, optimization_gate_set=basis,
                                           logical_before=logical_before,
                                           logical_after=logical_after[basis, optimizer],
                                           native_before=native_before, native_after=native_after[basis, optimizer],
                                           compiled_before=baseline[seed], compiled_after=after, sites=sites))
                    if len(paired) == len(routing_seeds):
                        trials.extend(paired)
    if not trials:
        raise ValueError("No verified comparisons fit the selected backends.")
    summaries = _summarize_trials(trials)
    pd.DataFrame({**flatten_point(row), "physical_sites": row["sites"],
                  "compilation_level": row["compilation_level"]} for row in trials).to_csv(
        output / "expanded_trials.csv", index=False)
    (output / "expanded_summary.json").write_text(json.dumps(summaries, indent=2, allow_nan=False))
    metadata = {"source_sha256": {name: item["sha256"] for name, item in inputs.items()},
                "backends": list(devices), "compilation_levels": compilation_levels, "routing_seeds": routing_seeds, "common_only": common_only,
                "logical_equivalence_exclusions": exclusions,
                "missing_input_circuits": missing,
                "compilation_equivalence_exclusions": compilation_exclusions,
                "quartz_search": "Nam preprocessing and native bounded total-gate search" if quartz_optimize else None,
                "quartz_seconds": quartz_seconds if quartz_optimize or quartz_rigetti_ecc else None,
                "rigetti_voqc_pipeline": "VOQC Nam optimization followed by Rigetti basis translation"
                if "Rigetti" in candidates_by_basis else None,
                "quartz_rigetti_search": "Rigetti basis translation, RZ merging, and bounded gate-count search"
                if quartz_rigetti_ecc else None,
                "quartz_rigetti_ecc_sha256": hashlib.sha256(quartz_rigetti_ecc.read_bytes()).hexdigest()
                if quartz_rigetti_ecc else None,
                "quartz_ecc_sha256": hashlib.sha256(quartz_ecc.read_bytes()).hexdigest() if quartz_optimize else None}
    (output / "expanded_policy.json").write_text(json.dumps(metadata, indent=2))
    print(f"{len(summaries)} comparisons summarized from {len(trials)} paired seed runs")
    return summaries
