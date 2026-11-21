"""Run routing-only compilation for every comparison in the published chart."""
import hashlib
import json
import re
from pathlib import Path
from statistics import median

from qiskit import qasm2
from .comparison import BASIS, _equivalent, metrics
from .controlled_compilation import controlled_compile, SEEDS, ROUTING_TRIALS
from .logical_prediction_helpers import optimize_suite, native_translation_metrics
from .logical_prediction_v2 import EXPANDED_BACKENDS, _connected_target
from .quartz_nam import _build_driver

ROOT = Path(__file__).resolve().parents[1]


def load_points():
    source = (ROOT / "docs/chart.html").read_text()
    return json.loads(re.search(r"const points = \((.*?)\)\.filter", source)[1])


def candidate_for(point, source):
    name, optimizer = point["circuit"], point["optimizer"]
    basis = point.get("optimization_gate_set", "Nam RZ")
    gates = BASIS if basis == "Nam RZ" else ["rz", "sx", "x", "cx"]
    if optimizer == "Qiskit":
        runs, excluded = optimize_suite({name: {"circuit": source}}, voqc_home=None,
                                       output=None, basis=gates, optimizers=("Qiskit",))
        if excluded:
            raise ValueError(excluded)
        candidate = runs[name, optimizer]["circuit"]
    else:
        candidates = []
        if optimizer == "VOQC":
            paths = (ROOT / "outputs/logical_prediction").glob(f"**/optimized/{name}_VOQC/logical.qasm")
            for path in paths:
                candidate = qasm2.load(path, custom_instructions=qasm2.LEGACY_CUSTOM_INSTRUCTIONS)
                if metrics(candidate) == point["logical_after"]:
                    candidates.append(candidate)
        else:
            quartz_cache = ROOT / "outputs/logical_prediction/quartz_cache"
            ecc = ROOT / "outputs/logical_prediction/dependencies/quartz/eccset/Nam_5_3_complete_ECC_set.json"
            _, current_driver = _build_driver(ecc, quartz_cache)
            paths = list(quartz_cache.glob(f"{name}-*.json"))
            paths.sort(key=lambda path: json.loads(path.read_text())["policy"]["driver_sha256"] == current_driver)
            for path in paths:
                entry = json.loads(path.read_text())
                if entry["policy"].get("optimization_gate_set", "Nam RZ") != basis:
                    continue
                candidate = qasm2.loads(entry["optimized"], custom_instructions=qasm2.LEGACY_CUSTOM_INSTRUCTIONS)
                if metrics(candidate) == point["logical_after"]:
                    candidates.append(candidate)
        if not candidates:
            raise ValueError(f"No verified cached candidate for {name} / {optimizer} / {basis}")
        # Prefer the current Quartz runtime when equivalent cache variants exist.
        candidate = candidates[-1]
    if metrics(candidate) != point["logical_after"]:
        raise ValueError(f"Logical candidate metrics differ: {name} / {optimizer} / {basis}")
    _equivalent(source, candidate)
    return candidate


def run():
    groups = {}
    for point in load_points():
        basis = point.get("optimization_gate_set", "Nam RZ")
        if basis not in ("Nam RZ", "IBM New") or point.get("compilation_level") == "routing":
            continue
        key = (point["circuit"], basis, point["optimizer"], point["backend"])
        groups.setdefault(key, point)
    output = ROOT / "docs/routing-only.json"
    cache = ROOT / "outputs/logical_prediction/routing_only"
    cache.mkdir(exist_ok=True)
    devices, patches, candidates, baselines = {}, {}, {}, {}
    results = []
    policy = {"pipeline": "fixed placement, lookahead SABRE, basis translation, direction correction",
              "calibration_free": True, "cleanup": False, "trials": ROUTING_TRIALS,
              "seeds": list(SEEDS),
              "driver_sha256": hashlib.sha256(Path(__file__).read_bytes() + Path(__file__).with_name("controlled_compilation.py").read_bytes()).hexdigest()}
    for index, (key, original) in enumerate(sorted(groups.items()), 1):
        name, basis, optimizer, backend = key
        candidate_key = name, basis, optimizer
        if candidate_key not in candidates:
            suite = "nam_rz" if basis == "Nam RZ" else "ibmnew"
            source = qasm2.load(ROOT / f"benchmarks/{suite}/{name}.qasm",
                                custom_instructions=qasm2.LEGACY_CUSTOM_INSTRUCTIONS)
            if metrics(source) != original["logical_before"]:
                raise ValueError(f"Source metrics differ: {key}")
            candidates[candidate_key] = source, candidate_for(original, source)
        source, candidate = candidates[candidate_key]
        if backend not in devices:
            devices[backend] = EXPANDED_BACKENDS[backend]()
        patch_key = backend, source.num_qubits
        if patch_key not in patches:
            patches[patch_key] = _connected_target(devices[backend], source.num_qubits)
        target, sites = patches[patch_key]
        fingerprint = hashlib.sha256((json.dumps(policy, sort_keys=True)+repr(key)+qasm2.dumps(source)+qasm2.dumps(candidate)+str(target)).encode()).hexdigest()
        path = cache / f"{fingerprint}.json"
        if path.exists():
            point = json.loads(path.read_text())
        else:
            trials = []
            for seed in SEEDS:
                before_key = name, basis, backend, seed
                if before_key not in baselines:
                    _, values = controlled_compile(source, source, target, seed)
                    baselines[before_key] = {**values, "duration_ns": None, "fidelity": None}
                _, values = controlled_compile(candidate, source, target, seed)
                trials.append({"seed": seed, "compiled_before": baselines[before_key],
                               "compiled_after": {**values, "duration_ns": None, "fidelity": None}})
            point = {k: v for k, v in original.items() if k not in ("gains", "spread", "compiled_before", "compiled_after")}
            point.update(compilation_level="routing", seeds=list(SEEDS), sites=sites,
                         compiled_before=trials[0]["compiled_before"], compiled_after=trials[0]["compiled_after"])
            point["native_before"] = native_translation_metrics(source, target)
            point["native_after"] = native_translation_metrics(candidate, target)
            point["gains"], point["spread"] = {}, {}
            for metric in trials[0]["compiled_before"]:
                if metric in ("duration_ns", "fidelity"):
                    continue
                values = [trial["compiled_before"][metric] - trial["compiled_after"][metric] for trial in trials]
                point["gains"][metric] = median(values)
                point["spread"][metric] = [min(values), max(values)]
            point["trials"] = trials
            path.write_text(json.dumps(point, allow_nan=False))
        results.append(point)
        print(f"{index}/{len(groups)} {key}: {point['gains']['gates']:+g}", flush=True)
        output.write_text(json.dumps({"policy": policy, "complete": index == len(groups),
                                     "expected_comparisons": len(groups), "points": results}, allow_nan=False))
    print("Finished", len(results), flush=True)


if __name__ == "__main__":
    run()
