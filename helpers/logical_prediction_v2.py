"""Aer validation and matched-seed superconducting benchmarks."""

import hashlib
import json
from collections import defaultdict
from statistics import median
from time import monotonic

import pandas as pd
from qiskit import qasm2
from qiskit.quantum_info import Operator, average_gate_fidelity
from qiskit.transpiler import Target
from qiskit_ibm_runtime.fake_provider import FakeJakartaV2, FakeSherbrooke

from .logical_prediction_helpers import (
    BACKENDS, compile_and_measure, flatten_point, load_suite, metrics, optimize_suite,
)


EXPANDED_BACKENDS = {**BACKENDS, "Jakarta": FakeJakartaV2, "Sherbrooke": FakeSherbrooke}
EXTRA_CIRCUITS = (
    "adder_4", "qaoa_n4_p4", "vqe_4", "vqe_uccsd_n4",
    "heisenberg_step_5_4", "heisenberg_step_20_4", "heisenberg_step_50_4",
    "TFIM_step_10_4", "TFIM_step_50_4", "TFIM_step_100_4", "XY_step_100_4",
    "tof_4", "barenco_tof_4", "hwb6", "qaoa_n6_p4", "qaoa_n8_p4",
    "vqe_nativegates_ibm_tket_8", "qgan_nativegates_ibm_tket_8",
    "qaoa_nativegates_ibm_tket_8", "XY_step_100_8", "TFIM_step_100_8",
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


def _quartz_optimizer(ecc_path, seconds):
    if not ecc_path.is_file():
        raise FileNotFoundError(f"Quartz Nam equivalence set missing: {ecc_path}")
    if seconds <= 0:
        raise ValueError("Quartz search time must be positive.")
    import quartz

    context = quartz.QuartzContext(
        gate_set=["h", "x", "rz", "cx", "add"], filename=str(ecc_path),
        no_increase=True, include_nop=False,
    )

    def optimize(circuit):
        graph = quartz.PyGraph.from_qasm_str(context=context, qasm_str=qasm2.dumps(circuit))
        deadline = monotonic() + seconds
        while monotonic() < deadline:
            improved = None
            for node in graph.all_nodes():
                if monotonic() >= deadline:
                    break
                for rewrite_id in sorted(graph.available_xfers(context=context, node=node)):
                    if monotonic() >= deadline:
                        break
                    rewrite = context.get_xfer_from_id(id=rewrite_id)
                    if rewrite.dst_gate_count >= rewrite.src_gate_count:
                        continue
                    candidate = graph.apply_xfer(xfer=rewrite, node=node)
                    if candidate is not None and candidate.gate_count < graph.gate_count:
                        improved = candidate
                        break
                if improved is not None:
                    break
            if improved is None:
                break
            graph = improved
        return qasm2.loads(graph.to_qasm_str(), custom_instructions=qasm2.LEGACY_CUSTOM_INSTRUCTIONS)

    return optimize


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
        groups[trial["circuit"], trial["backend"], trial["optimizer"]].append(trial)
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
        summaries.append({**group[0], "seeds": [row["seed"] for row in group],
                          "gains": gains, "spread": spread})
    return summaries


def run_expanded_benchmark(circuits, backends, routing_seeds, *, suite, voqc_home,
                           output, quartz_ecc=None, quartz_seconds=30, common_only=False):
    """Compile matched seeds and report medians of paired gains for each comparison."""
    routing_seeds = tuple(routing_seeds)
    if not routing_seeds or len(routing_seeds) != len(set(routing_seeds)):
        raise ValueError("Supply at least one routing seed and no duplicates.")
    devices = {name: EXPANDED_BACKENDS[name]() for name in backends}
    if not devices:
        raise ValueError("Select at least one backend.")
    inputs = load_suite(circuits, suite, min_width=4, max_width=8)
    if common_only:
        width_limit = min(device.num_qubits for device in devices.values())
        inputs = {name: item for name, item in inputs.items()
                  if item["circuit"].num_qubits <= width_limit}
    if not inputs:
        raise ValueError("No eligible inputs.")
    quartz_optimize = _quartz_optimizer(quartz_ecc, quartz_seconds) if quartz_ecc is not None else None
    output.mkdir(parents=True, exist_ok=True)
    optimized, exclusions = optimize_suite(inputs, voqc_home=voqc_home, output=output,
                                           quartz_optimize=quartz_optimize)
    patches = {}
    trials = []
    for name, item in inputs.items():
        source = item["circuit"]
        logical_before = metrics(source)
        candidates = {label: run["circuit"] for (key, label), run in optimized.items() if key == name}
        logical_after = {label: metrics(circuit) for label, circuit in candidates.items()}
        for backend_name, backend in devices.items():
            width = source.num_qubits
            if width > backend.num_qubits:
                continue
            if (backend_name, width) not in patches:
                patches[backend_name, width] = _connected_target(backend, width)
            target, sites = patches[backend_name, width]
            for seed in routing_seeds:
                _, before = compile_and_measure(source, source, target, seed)
                for optimizer, candidate in candidates.items():
                    _, after = compile_and_measure(candidate, source, target, seed)
                    trials.append(dict(circuit=name, backend=backend_name, optimizer=optimizer,
                                       seed=seed, logical_before=logical_before,
                                       logical_after=logical_after[optimizer],
                                       compiled_before=before, compiled_after=after, sites=sites))
    if not trials:
        raise ValueError("No verified comparisons fit the selected backends.")
    summaries = _summarize_trials(trials)
    pd.DataFrame({**flatten_point(row), "physical_sites": row["sites"]} for row in trials).to_csv(
        output / "expanded_trials.csv", index=False)
    (output / "expanded_summary.json").write_text(json.dumps(summaries, indent=2, allow_nan=False))
    metadata = {"source_sha256": {name: item["sha256"] for name, item in inputs.items()},
                "backends": list(devices), "routing_seeds": routing_seeds, "common_only": common_only,
                "logical_equivalence_exclusions": exclusions,
                "quartz_search": "first improving rewrite, total gates" if quartz_optimize else None,
                "quartz_seconds": quartz_seconds if quartz_optimize else None,
                "quartz_ecc_sha256": hashlib.sha256(quartz_ecc.read_bytes()).hexdigest() if quartz_optimize else None}
    (output / "expanded_policy.json").write_text(json.dumps(metadata, indent=2))
    print(f"{len(summaries)} comparisons summarized from {len(trials)} paired seed runs")
    return summaries
