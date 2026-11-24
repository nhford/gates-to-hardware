"""Verified adapter for QUESO's rewrite-only total-gate-count search."""
import hashlib
import json
import subprocess
import time
from pathlib import Path

from qiskit import qasm2

from .comparison import BASIS, _equivalent, metrics

ROOT = Path(__file__).resolve().parents[1]
CHECKOUT = ROOT / "outputs/logical_prediction/dependencies/guoq"
JAR = CHECKOUT / "target/GUOQ-1.0-jar-with-dependencies.jar"


def queso_optimize(name, source, basis, *, seconds=5):
    """Budget search time after rewrite-rule loading, preserving verified output."""
    gate_set, rule_name, gates = {
        "Nam RZ": ("NAM", "nam", set(BASIS)),
        "IBM New": ("IBMN", "ibmnew", {"rz", "sx", "x", "cx"}),
    }[basis]
    rules = [CHECKOUT / f"rules/rules_q3_s6_{rule_name}.txt",
             CHECKOUT / f"rules/rules_q3_s3_{rule_name}_symb.txt"]
    for path in [JAR, *rules]:
        if not path.is_file():
            raise FileNotFoundError(f"Missing QUESO dependency: {path}")
    original = qasm2.dumps(source)
    policy = {
        "optimizer": "QUESO", "basis": basis, "objective": "TOTAL",
        "search": "BEAM", "temperature": 0, "queue_size": 8000, "seed": 0,
        "resynthesis": "NONE", "search_seconds": seconds,
        "revision": subprocess.check_output(["git", "-C", str(CHECKOUT), "rev-parse", "HEAD"], text=True).strip(),
        "source_sha256": hashlib.sha256(original.encode()).hexdigest(),
        "jar_sha256": hashlib.sha256(JAR.read_bytes()).hexdigest(),
        "rule_sha256": [hashlib.sha256(path.read_bytes()).hexdigest() for path in rules],
    }
    digest = hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()
    folder = ROOT / "outputs/logical_prediction/queso_cache" / f"{name}-{digest}"
    folder.mkdir(parents=True, exist_ok=True)
    cached = folder / "verified.json"
    if cached.exists():
        result = json.loads(cached.read_text())
        candidate = qasm2.loads(result["qasm"], custom_instructions=qasm2.LEGACY_CUSTOM_INSTRUCTIONS)
        if set(candidate.count_ops()) - gates or len(candidate.data) > len(source.data):
            raise ValueError("Cached QUESO output violates its gate-set or cost policy.")
        _equivalent(source, candidate)
        return candidate, result["policy"]
    input_path = folder / "input.qasm"
    input_path.write_text(original)
    output_path = folder / "latest_sol__input.qasm"
    command = [
        "java", "-Xmx2g", "-cp", str(JAR), "qoptimizer.Optimizer",
        "-g", gate_set, "-opt", "TOTAL", "-search", "BEAM", "-temp", "0",
        "-q", "8000", "-resynth", "NONE", "--seed", "0", "--verbosity", "2",
        "-r", str(rules[0]), "-sr", str(rules[1]), "-out", str(folder), str(input_path),
    ]
    log_path = folder / "optimizer.log"
    latest = original
    search_started = None
    terminated = False
    output_stamp = None
    launched = time.monotonic()
    with log_path.open("w") as log:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, cwd=CHECKOUT)
        try:
            while process.poll() is None:
                now = time.monotonic()
                if search_started is None and "Total rules:" in log_path.read_text():
                    search_started = now
                if output_path.exists() and output_path.stat().st_mtime_ns != output_stamp:
                    output_stamp = output_path.stat().st_mtime_ns
                    text = output_path.read_text()
                    try:
                        qasm2.loads(text, custom_instructions=qasm2.LEGACY_CUSTOM_INSTRUCTIONS)
                        latest = text
                    except qasm2.QASM2Error:
                        pass
                if search_started is not None and now - search_started >= seconds:
                    terminated = True
                    process.terminate()
                    break
                if search_started is None and now - launched > 120:
                    raise RuntimeError(f"QUESO rule loading timed out: {log_path}")
                time.sleep(0.05)
            process.wait(timeout=10)
            if not terminated and process.returncode != 0:
                raise RuntimeError(f"QUESO failed: {log_path}")
            if output_path.exists():
                text = output_path.read_text()
                try:
                    qasm2.loads(text, custom_instructions=qasm2.LEGACY_CUSTOM_INSTRUCTIONS)
                    latest = text
                except qasm2.QASM2Error:
                    pass
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
    candidate = qasm2.loads(latest, custom_instructions=qasm2.LEGACY_CUSTOM_INSTRUCTIONS)
    if set(candidate.count_ops()) - gates:
        raise ValueError(f"QUESO output is outside {basis}")
    _equivalent(source, candidate)
    if metrics(candidate)["gates"] > metrics(source)["gates"]:
        raise ValueError("QUESO total-gate search increased the logical gate count")
    cached.write_text(json.dumps({"policy": policy, "qasm": qasm2.dumps(candidate)}, indent=2))
    return candidate, policy


def queso_optimizer(basis, *, seconds=5):
    """Configure QUESO for optimize_suite's callable optimizer interface."""
    def optimize(name, source):
        candidate, _ = queso_optimize(name, source, basis, seconds=seconds)
        return candidate
    return optimize
