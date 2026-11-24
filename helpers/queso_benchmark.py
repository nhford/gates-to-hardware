"""Add QUESO comparisons to all existing Nam RZ and IBM New hardware views.

Run with python -m helpers.queso_benchmark from the repository root.
"""
import concurrent.futures
import csv
import hashlib
import json
from pathlib import Path
from statistics import median

from qiskit import qasm2

from .comparison import metrics
from .controlled_compilation import controlled_compile, SEEDS
from .logical_prediction_helpers import compile_and_measure, native_translation_metrics
from .logical_prediction_v2 import EXPANDED_BACKENDS, _connected_target
from .queso import queso_optimize
from .routing_benchmark import load_points

ROOT = Path(__file__).resolve().parents[1]
METRICS = ("gates", "2Q", "depth", "swaps", "duration_ns", "fidelity")


def compile_group(job):
    """One fixed circuit/basis/backend pair, with all published hardware modes."""
    name, basis, backend, source_qasm, candidate_qasm, templates, baseline_rows, policy = job
    source = qasm2.loads(source_qasm, custom_instructions=qasm2.LEGACY_CUSTOM_INSTRUCTIONS)
    candidate = qasm2.loads(candidate_qasm, custom_instructions=qasm2.LEGACY_CUSTOM_INSTRUCTIONS)
    device = EXPANDED_BACKENDS[backend]()
    target, sites = _connected_target(device, source.num_qubits)
    fingerprint = hashlib.sha256(
        (json.dumps(policy, sort_keys=True) + str(templates) + str(baseline_rows)
         + str(target) + source_qasm + candidate_qasm).encode()
    ).hexdigest()
    cache = ROOT / "outputs/logical_prediction/queso_compiled"
    cache.mkdir(exist_ok=True)
    path = cache / f"{fingerprint}.json"
    if path.exists():
        return json.loads(path.read_text())
    native_before = native_translation_metrics(source, target)
    native_after = native_translation_metrics(candidate, target)
    output, exclusions = [], []
    for mode, template in templates.items():
        trials = []
        for seed in SEEDS:
            if mode == "routing":
                before = baseline_rows["routing"][str(seed)]
                _, values = controlled_compile(candidate, source, target, seed)
                after = {metric: values.get(metric) for metric in METRICS}
            else:
                before = baseline_rows[str(mode)][str(seed)]
                try:
                    _, after = compile_and_measure(candidate, source, target, seed, optimization_level=int(mode))
                except ValueError as error:
                    if "equivalence" not in str(error):
                        raise
                    exclusions.append({"circuit": name, "basis": basis, "backend": backend,
                                       "compilation_level": mode, "seed": seed, "reason": str(error)})
                    break
            trials.append({"seed": seed, "compiled_before": before, "compiled_after": after})
        if len(trials) != len(SEEDS):
            continue
        point = {
            "circuit": name, "backend": backend, "optimizer": "QUESO",
            "optimization_gate_set": basis, "compilation_level": mode,
            "seed": 0, "seeds": list(SEEDS), "sites": sites,
            "logical_before": metrics(source), "logical_after": metrics(candidate),
            "native_before": native_before, "native_after": native_after,
            "compiled_before": trials[0]["compiled_before"],
            "compiled_after": trials[0]["compiled_after"],
            "gains": {}, "spread": {}, "trials": trials,
        }
        for metric in METRICS:
            if any(trial["compiled_before"].get(metric) is None or trial["compiled_after"].get(metric) is None for trial in trials):
                continue
            values = [
                100 * (trial["compiled_after"][metric] - trial["compiled_before"][metric])
                if metric == "fidelity" else trial["compiled_before"][metric] - trial["compiled_after"][metric]
                for trial in trials
            ]
            point["gains"][metric] = median(values)
            point["spread"][metric] = [min(values), max(values)]
        output.append(point)
    result = {"points": output, "exclusions": exclusions}
    path.write_text(json.dumps(result, allow_nan=False))
    return result


def run():
    existing = [point for point in load_points()
                if point["optimizer"] != "QUESO"
                and point.get("optimization_gate_set") in ("Nam RZ", "IBM New")]
    groups = {}
    for point in existing:
        key = point["circuit"], point["optimization_gate_set"], point["backend"]
        groups.setdefault(key, {}).setdefault(point["compilation_level"], point)
    with (ROOT / "docs/expanded_trials.csv").open() as handle:
        raw = {}
        for row in csv.DictReader(handle):
            raw[(row["circuit"], row["optimization_gate_set"], row["backend"],
                 row["optimizer"], int(row["compilation_level"]), int(row["seed"]))] = row
    routing = {}
    for point in json.loads((ROOT / "docs/routing-only.json").read_text())["points"]:
        routing.setdefault((point["circuit"], point["optimization_gate_set"], point["backend"]), point)
    policies, circuits = {}, {}
    errors = []
    logical_keys = sorted({key[:2] for key in groups})
    for index, (name, basis) in enumerate(logical_keys, 1):
        suite = "nam_rz" if basis == "Nam RZ" else "ibmnew"
        source = qasm2.load(ROOT / f"benchmarks/{suite}/{name}.qasm",
                           custom_instructions=qasm2.LEGACY_CUSTOM_INSTRUCTIONS)
        try:
            candidate, policy = queso_optimize(name, source, basis)
        except ValueError as error:
            if "equivalence" not in str(error):
                raise
            errors.append({"circuit": name, "basis": basis, "reason": str(error)})
            print(f"Excluded logical output: {name} / {basis}: {error}", flush=True)
            continue
        circuits[name, basis] = qasm2.dumps(source), qasm2.dumps(candidate)
        policies[name + " / " + basis] = policy
        print(f"Logical {index}/{len(logical_keys)} {name} / {basis}: {len(source.data)} → {len(candidate.data)}", flush=True)
    jobs = []
    for (name, basis, backend), templates in sorted(groups.items()):
        if (name, basis) not in circuits:
            continue
        baselines = {}
        for mode, template in templates.items():
            values = {}
            for seed in SEEDS:
                if mode == "routing":
                    trial = next(t for t in routing[name, basis, backend]["trials"] if t["seed"] == seed)
                    values[str(seed)] = {metric: trial["compiled_before"].get(metric) for metric in METRICS}
                else:
                    row = raw[name, basis, backend, template["optimizer"], int(mode), seed]
                    values[str(seed)] = {metric: float(row["compiled_before_" + metric])
                                         if row["compiled_before_" + metric] else None for metric in METRICS}
            baselines[str(mode)] = values
        source, candidate = circuits[name, basis]
        jobs.append((name, basis, backend, source, candidate, templates, baselines, policies[name + " / " + basis]))
    payload = {
        "complete": False, "expected_hardware_groups": len(jobs), "policies": policies,
        "logical_outputs": {name + " / " + basis: candidate
                            for (name, basis), (_, candidate) in circuits.items()},
        "points": [], "exclusions": errors,
    }
    destination = ROOT / "docs/queso-results.json"
    with concurrent.futures.ProcessPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(compile_group, job): job[:3] for job in jobs}
        for index, future in enumerate(concurrent.futures.as_completed(futures), 1):
            result = future.result()
            payload["points"].extend(result["points"])
            payload["exclusions"].extend(result["exclusions"])
            payload["complete"] = index == len(jobs)
            destination.write_text(json.dumps(payload, allow_nan=False))
            print(f"Hardware {index}/{len(jobs)} {futures[future]}: {len(result['points'])} modes", flush=True)
    print("Finished", len(payload["points"]), "comparisons;", len(payload["exclusions"]), "exclusions", flush=True)


if __name__ == "__main__":
    run()
