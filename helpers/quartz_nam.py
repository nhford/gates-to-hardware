"""Native Quartz adapter with a content-addressed, verified circuit cache."""

import hashlib
import json
import re
import subprocess
from math import pi
from pathlib import Path
from tempfile import TemporaryDirectory

from qiskit import qasm2, transpile
from qiskit.circuit.library import CCZGate, RXGate

from .comparison import BASIS, _equivalent

DRIVER = Path(__file__).with_suffix(".cpp")
CCZ_INPUTS = DRIVER.parents[1] / "benchmarks" / "quartz_ccz"
RIGETTI_BASIS = ("rx", "rz", "cz")
RIGETTI_FIXED_ROTATIONS = {"pi/2": "rx1", "-pi/2": "rx3", "pi": "x", "-pi": "x"}
NAM_INSTRUCTIONS = qasm2.LEGACY_CUSTOM_INSTRUCTIONS
RIGETTI_INSTRUCTIONS = (*NAM_INSTRUCTIONS,
                        qasm2.CustomInstruction("rx1", 0, 1, lambda: RXGate(pi / 2), builtin=True),
                        qasm2.CustomInstruction("rx3", 0, 1, lambda: RXGate(-pi / 2), builtin=True))
CCZ_INSTRUCTIONS = (*NAM_INSTRUCTIONS, qasm2.CustomInstruction("ccz", 0, 3, CCZGate, builtin=True))


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _build_driver(ecc_path, cache):
    checkout = next((path for path in ecc_path.parents if (path / "src/quartz").is_dir()),
                    ecc_path.parent.parent)
    runtime_dir = checkout / "build-float"
    runtime = next(iter(sorted(runtime_dir.glob("libquartz_runtime.*"))), None)
    if runtime is None:
        raise FileNotFoundError(f"Build the floating-point Quartz runtime in {runtime_dir}")
    fingerprint = _digest(DRIVER.read_bytes() + runtime.read_bytes())
    binary = cache / f"quartz-nam-{fingerprint}"
    if not binary.is_file():
        with TemporaryDirectory(dir=cache) as folder:
            temporary = Path(folder) / "quartz-nam"
            subprocess.run([
                "c++", "-O2", "-std=c++17", f"-I{checkout / 'src'}", str(DRIVER),
                str(runtime), f"-Wl,-rpath,{runtime_dir}", "-o", str(temporary),
            ], check=True)
            temporary.replace(binary)
    return binary, fingerprint


def _convert_legacy_rigetti_ecc(data):
    """Preserve the upstream verifier classes and translate local parameter IDs."""
    links, classes = json.loads(data)
    if links:
        raise ValueError("Expected Rigetti classes with verifier links already resolved.")
    symbolic_count = max(header[1] for circuits in classes.values() for header, _ in circuits)
    expressions = {}
    for key, circuits in classes.items():
        converted = []
        for (width, num_params, _, _, *_), gates in circuits:
            param_ids = {f"P{i}": f"P{i}" for i in range(num_params)}
            quantum_gates = []
            for gate, outputs, inputs in gates:
                if gate == "add":
                    operands = tuple(param_ids[param] for param in inputs)
                    if operands not in expressions:
                        expressions[operands] = symbolic_count + len(expressions)
                    param_ids[outputs[0]] = f"P{expressions[operands]}"
                else:
                    quantum_gates.append([gate, outputs, [param_ids.get(wire, wire) for wire in inputs]])
            converted.append([[width, len(quantum_gates)], quantum_gates])
        classes[key] = converted
    param_info = [symbolic_count + len(expressions), *[""] * symbolic_count,
                  *[["add", [f"P{index}"], list(operands)] for operands, index in expressions.items()]]
    # Quartz hashes symbolic circuits at fixed sample angles when loading classes.
    samples = [0.37 + i * 0.89 for i in range(symbolic_count)]
    return json.dumps([[param_info, [symbolic_count, *samples]], classes])


def quartz_optimizer(ecc_path, seconds, cache, *, rigetti=False):
    ecc_path = ecc_path.resolve(strict=True)
    if seconds <= 0:
        raise ValueError("Quartz search time must be positive.")
    cache.mkdir(parents=True, exist_ok=True)
    binary, driver_hash = _build_driver(ecc_path, cache)
    ecc_data = ecc_path.read_bytes()
    if rigetti:
        ecc_data = _convert_legacy_rigetti_ecc(ecc_data).encode()
    ecc_hash = _digest(ecc_data)

    def optimize(name, source):
        original = qasm2.dumps(source)
        ccz_path = CCZ_INPUTS / f"{name}.qasm"
        has_ccz = not rigetti and ccz_path.is_file()
        raw = ccz_path.read_text() if has_ccz else original
        if rigetti:
            translated = transpile(source, basis_gates=RIGETTI_BASIS, optimization_level=0)
            raw = qasm2.dumps(translated)
            raw = re.sub(r"(?m)^rx\((-?pi/2|-?pi)\)",
                         lambda match: RIGETTI_FIXED_ROTATIONS[match[1]], raw)
            _equivalent(source, qasm2.loads(raw, custom_instructions=RIGETTI_INSTRUCTIONS))
        if has_ccz:
            _equivalent(source, qasm2.loads(raw, custom_instructions=CCZ_INSTRUCTIONS))
        policy = {
            "source_sha256": _digest(original.encode()), "input_sha256": _digest(raw.encode()),
            "driver_sha256": driver_hash, "ecc_sha256": ecc_hash, "search_seconds": seconds,
        }
        if rigetti:
            policy["optimization_gate_set"] = "Rigetti"
        optimizer_label = "Quartz / Rigetti" if rigetti else "Quartz"
        key = _digest(json.dumps(policy, sort_keys=True).encode())
        entry = cache / f"{name}-{key}.json"

        def verify(result):
            circuits = [qasm2.loads(result[stage], custom_instructions=NAM_INSTRUCTIONS)
                        for stage in ("preprocessed", "optimized")]
            for circuit in circuits:
                if set(circuit.count_ops()) - set(RIGETTI_BASIS if rigetti else BASIS):
                    raise ValueError(f"{name}: Quartz output is outside the optimization basis")
                _equivalent(source, circuit)
            return circuits

        if entry.is_file():
            result = json.loads(entry.read_text())
            if result["policy"] != policy:
                raise ValueError(f"Quartz cache policy mismatch: {entry}")
            preprocessed, optimized = verify(result)
            print(f"{name} / {optimizer_label} cached: {preprocessed.size()} → {optimized.size()} gates")
            return optimized

        with TemporaryDirectory(dir=cache) as folder:
            directory = Path(folder)
            input_path, preprocessed_path, output_path = (
                directory / filename for filename in ("input.qasm", "preprocessed.qasm", "optimized.qasm")
            )
            input_path.write_text(raw)
            rules_path = ecc_path
            if rigetti:
                rules_path = directory / "ecc.json"
                rules_path.write_bytes(ecc_data)
            subprocess.run([
                str(binary), str(input_path), str(rules_path), str(preprocessed_path),
                str(output_path), str(seconds), str(int(has_ccz)),
                *(["rigetti"] if rigetti else []),
            ], check=True)
            result = {"policy": policy}
            for stage, path in (("preprocessed", preprocessed_path), ("optimized", output_path)):
                qasm = path.read_text()
                if rigetti:
                    circuit = qasm2.loads(qasm, custom_instructions=RIGETTI_INSTRUCTIONS)
                    qasm = qasm2.dumps(transpile(circuit, basis_gates=RIGETTI_BASIS, optimization_level=0))
                result[stage] = qasm
            preprocessed, optimized = verify(result)
            temporary = directory / "result.json"
            temporary.write_text(json.dumps(result, indent=2))
            temporary.replace(entry)
        print(f"{name} / {optimizer_label}: {source.size()} → {preprocessed.size()} → {optimized.size()} gates")
        return optimized

    return optimize
