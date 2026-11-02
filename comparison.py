"""Logical optimizer comparison followed by a shared hardware compilation policy."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import time

os.environ.setdefault('QISKIT_NUM_PROCS', '1')
import pandas as pd
from qiskit import qasm2, qpy, transpile
from qiskit.quantum_info import Operator
from qiskit.transpiler import PassManager
from qiskit.transpiler.passes import BasisTranslator
from qiskit.circuit.equivalence_library import SessionEquivalenceLibrary
from qiskit_ibm_runtime.fake_provider import FakeLimaV2, FakeManilaV2

ROOT = Path(__file__).resolve().parent
BASIS = ['h', 'x', 'rz', 'cx']
BACKENDS = {'Manila': FakeManilaV2, 'Lima': FakeLimaV2}
COLUMNS = ['Circuit', 'Optimizer', 'Backend', 'Logical gates', 'Logical 2Q',
           'Logical depth', 'Compiled gates', 'Compiled 2Q', 'Compiled depth']


def metrics(circuit):
    return {'gates': circuit.size(),
            '2Q': sum(len(item.qubits) == 2 for item in circuit.data),
            'depth': circuit.depth()}


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _json(path, data):
    Path(path).write_text(json.dumps(data, indent=2, default=str) + '\n')


def _save(circuit, path):
    with Path(path).open('wb') as handle:
        qpy.dump(circuit, handle)


def _equivalent(source, candidate, physical=False):
    if source.num_qubits != candidate.num_qubits:
        raise ValueError('This pilot requires equal circuit and backend widths (five qubits).')
    target = Operator.from_circuit(candidate) if physical else Operator(candidate)
    # Synthesis is numerical. Compare the complete unitary up to global phase.
    if not Operator(source).equiv(target, rtol=0, atol=1e-6):
        raise ValueError('Circuit failed full-unitary equivalence at tolerance 1e-6.')


def load_inputs(circuits):
    inputs = {}
    for name in circuits:
        path = ROOT / 'benchmarks' / 'nam_rz' / f'{name}.qasm'
        source = qasm2.load(path, custom_instructions=qasm2.LEGACY_CUSTOM_INSTRUCTIONS)
        if source.num_qubits != 5 or any(i.operation.name not in BASIS for i in source.data):
            raise ValueError(f'{name}: use a five-qubit unitary circuit in the Nam basis {BASIS}.')
        inputs[name] = {'path': path, 'circuit': source, 'sha256': _sha(path)}
    return inputs


def input_summary(inputs):
    return pd.DataFrame([{'Circuit': name, 'Qubits': item['circuit'].num_qubits,
                          **metrics(item['circuit'])} for name, item in inputs.items()])


def _translate(circuit, basis):
    return PassManager([BasisTranslator(SessionEquivalenceLibrary, basis)]).run(circuit)


def optimize(inputs, *, voqc_home, qiskit_level=3, force=False, output=ROOT / 'outputs'):
    home = Path(voqc_home).expanduser().resolve()
    library = home / 'pyvoqc/lib/libvoqc.so'
    wrapper = home / 'pyvoqc/voqc.py'
    for path in (library, wrapper):
        if not path.is_file():
            raise FileNotFoundError(f'Missing built VOQC dependency: {path}. See README.md.')
    if qiskit_level not in range(4):
        raise ValueError('Use Qiskit level 0–3.')
    output = Path(output).resolve()
    versions = {p: importlib.metadata.version(p) for p in
                ('qiskit', 'qiskit-ibm-runtime', 'numpy')}
    engine = {'library_sha256': _sha(library), 'wrapper_sha256': _sha(wrapper)}
    optimized = {}
    for name, item in inputs.items():
        for optimizer in ('VOQC', 'Qiskit'):
            policy = {'optimizer': optimizer, 'source_sha256': item['sha256'], 'basis': BASIS,
                      'versions': versions, 'adapter_sha256': _sha(__file__)}
            if optimizer == 'VOQC':
                policy.update(engine=engine, pipeline=['optimize_nam', 'replace_rzq'],
                              input_basis=['h', 'x', 'rz', 'cx'],
                              translation='BasisTranslator only to normalize output to Nam')
            else:
                policy.update(level=qiskit_level, seed=0, approximation_degree=1.0)
            folder = output / 'optimized' / f'{name}_{optimizer}'
            cached, meta_path = folder / 'logical.qpy', folder / 'metadata.json'
            meta = json.loads(meta_path.read_text()) if meta_path.is_file() else {}
            if not force and cached.is_file() and meta.get('policy') == policy:
                if meta['qpy_sha256'] != _sha(cached):
                    raise ValueError(f'Cache checksum mismatch: {cached}')
                with cached.open('rb') as handle:
                    candidate = qpy.load(handle)[0]
            else:
                folder.mkdir(parents=True, exist_ok=True)
                start = time.perf_counter()
                if optimizer == 'VOQC':
                    prepared = item['circuit'].copy()
                    _equivalent(item['circuit'], prepared)
                    in_path, out_path = folder / 'voqc_input.qasm', folder / 'voqc_output.qasm'
                    qasm2.dump(prepared, in_path)
                    out_path.unlink(missing_ok=True)
                    launch = """import sys
sys.path.insert(0, sys.argv[1])
from pyvoqc.voqc import get_library_handle, VOQCCircuit
circuit = VOQCCircuit(get_library_handle(), sys.argv[2])
circuit.optimize_nam()
circuit.replace_rzq()
circuit.write(sys.argv[3])
"""
                    command = [sys.executable, '-c', launch, str(home), str(in_path), str(out_path)]
                    with (folder / 'voqc.log').open('w') as log:
                        result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
                    if result.returncode or not out_path.is_file():
                        raise RuntimeError(f'VOQC failed. See {folder / "voqc.log"}')
                    raw = qasm2.load(out_path, custom_instructions=qasm2.LEGACY_CUSTOM_INSTRUCTIONS)
                    _equivalent(item['circuit'], raw)
                    candidate = _translate(raw, BASIS)
                    details = {'prepared_metrics': metrics(prepared), 'raw_output_metrics': metrics(raw),
                               'log': os.path.relpath(folder / 'voqc.log', ROOT)}
                else:
                    candidate = transpile(item['circuit'], basis_gates=BASIS,
                                          optimization_level=qiskit_level, seed_transpiler=0,
                                          approximation_degree=1.0)
                    details = {}
                elapsed = time.perf_counter() - start
                if any(i.operation.name not in BASIS for i in candidate.data):
                    raise ValueError(f'{optimizer} output is not in the common Nam basis.')
                _equivalent(item['circuit'], candidate)
                _save(candidate, cached)
                meta = {'policy': policy, 'seconds': elapsed, 'metrics': metrics(candidate),
                        'qpy_sha256': _sha(cached), 'equivalent': True, **details}
                _json(meta_path, meta)
            _equivalent(item['circuit'], candidate)
            qasm_path = folder / 'logical.qasm'
            qasm2.dump(candidate, qasm_path)
            exported = qasm2.load(qasm_path)
            _equivalent(candidate, exported)
            meta.update(qasm_file=os.path.relpath(qasm_path, ROOT),
                        qasm_sha256=_sha(qasm_path), qasm_equivalent=True)
            _json(meta_path, meta)
            optimized[name, optimizer] = {'circuit': candidate, 'metadata': meta,
                                          'cache': os.path.relpath(cached, ROOT)}
            print(f'{name} / {optimizer}: {metrics(candidate)}')
    return optimized


def compile_results(inputs, optimized, *, backends=('Manila', 'Lima'), seed=0,
                    output=ROOT / 'outputs'):
    if seed != 0 or not backends:
        raise ValueError('This experiment uses compilation seed 0 and requires backends.')
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    rows, trials, snapshots = [], [], {}
    for backend_name in backends:
        backend = BACKENDS[backend_name]()
        snapshots[backend_name] = {'configuration': backend.configuration().to_dict(),
                                  'properties': backend.properties().to_dict()}
        for (name, optimizer), item in optimized.items():
            logical = item['circuit']
            start = time.perf_counter()
            physical = transpile(logical, backend=backend, optimization_level=1,
                                 initial_layout=list(range(5)), routing_method='sabre',
                                 seed_transpiler=seed, approximation_degree=1.0)
            elapsed = time.perf_counter() - start
            _equivalent(inputs[name]['circuit'], physical, physical=True)
            path = output / 'compiled' / f'{name}_{optimizer}_{backend_name}_seed{seed}.qpy'
            path.parent.mkdir(parents=True, exist_ok=True)
            _save(physical, path)
            qasm_path = path.with_suffix('.qasm')
            qasm_text = qasm2.dumps(physical)
            if 'sx' in physical.count_ops() and 'gate sx ' not in qasm_text:
                qasm_text = qasm_text.replace('include "qelib1.inc";',
                    'include "qelib1.inc";\ngate sx a { h a; s a; h a; }', 1)
            qasm_path.write_text(qasm_text)
            exported = qasm2.load(qasm_path)
            if not Operator(physical).equiv(Operator(exported), rtol=0, atol=1e-6):
                raise ValueError(f'QASM round-trip changed the physical circuit: {qasm_path}')
            record = {'circuit': name, 'optimizer': optimizer, 'backend': backend_name,
                      'seed': seed, **metrics(physical), 'compile_seconds': elapsed,
                      'duration_ns': physical.estimate_duration(backend.target, unit='s') * 1e9,
                      'equivalent': True, 'file': os.path.relpath(path, ROOT),
                      'qasm_file': os.path.relpath(qasm_path, ROOT), 'selected': True}
            trials.append(record)
            rows.append({'Circuit': name, 'Optimizer': optimizer, 'Backend': backend_name,
                         **{f'Logical {k}': v for k, v in metrics(logical).items()},
                         **{f'Compiled {k}': record[k] for k in ('gates', '2Q', 'depth')}})
    table = pd.DataFrame(rows, columns=COLUMNS).sort_values(['Circuit', 'Optimizer', 'Backend']).reset_index(drop=True)
    table.to_csv(output / 'results.csv', index=False)
    pd.DataFrame(trials).to_csv(output / 'trials.csv', index=False)
    _json(output / 'metadata.json', {
        'versions': {p: importlib.metadata.version(p) for p in ('qiskit', 'qiskit-ibm-runtime', 'numpy', 'pandas')},
        'python': sys.version, 'created_utc': pd.Timestamp.now(tz='UTC').isoformat(),
        'inputs': {n: {'path': str(v['path']), 'sha256': v['sha256'],
                       'qubits': v['circuit'].num_qubits, **metrics(v['circuit'])} for n, v in inputs.items()},
        'logical_runs': {f'{n}/{o}': v['metadata'] for (n,o),v in optimized.items()},
        'compile_policy': {'level': 1, 'router': 'sabre', 'initial_layout': list(range(5)),
                           'seed': seed, 'selection': 'single seed 0 run'},
        'equivalence_atol': 1e-6, 'backends': snapshots, 'trials': trials,
        'notes': 'Historical device snapshots; no hardware execution or noisy simulation. VOQC optimize_nam, output normalized to Nam; Qiskit level 3. No VOQC routing or hardware mapping.'})
    return table


def show_comparison(frame):
    """Clickable sorting/filtering with shared logical cells merged after each draw."""
    from itables import JavascriptFunction, init_notebook_mode, show

    merge = JavascriptFunction("""function () {
        const api = this.api();
        const columns = [0, 1, 3, 4, 5];
        api.rows().every(function () {
            const row = this.index();
            columns.forEach(col => {
                const cell = api.cell(row, col).node();
                if (cell) { cell.rowSpan = 1; cell.style.display = ''; }
            });
        });
        const rows = api.rows({page: 'current', search: 'applied', order: 'applied'}).indexes().toArray();
        let start = 0;
        while (start < rows.length) {
            const first = api.row(rows[start]).data();
            let end = start + 1;
            while (end < rows.length) {
                const next = api.row(rows[end]).data();
                if (!columns.every(col => first[col] === next[col])) break;
                end++;
            }
            if (end - start > 1) {
                columns.forEach(col => {
                    const cell = api.cell(rows[start], col).node();
                    cell.rowSpan = end - start;
                    cell.style.verticalAlign = 'middle';
                    for (let i = start + 1; i < end; i++) {
                        api.cell(rows[i], col).node().style.display = 'none';
                    }
                });
            }
            start = end;
        }
    }""")
    ordered = frame[COLUMNS].sort_values(['Circuit', 'Optimizer', 'Backend'])
    init_notebook_mode(all_interactive=False, connected=False)
    show(ordered, showIndex=False, classes='display compact', column_filters='header',
         style='width:100%;font-size:12px',
         css='table.compact thead input {width:100%;max-width:100px;min-width:0;box-sizing:border-box;}',
         paging=False, order=[[0, 'asc'], [1, 'asc'], [2, 'asc']], drawCallback=merge,
         layout={'topStart': None, 'topEnd': 'search', 'bottomStart': 'info', 'bottomEnd': None})


def compare_workflows(inputs, *, backends=('Manila', 'Lima'), seed=0,
                      output=ROOT / 'outputs' / 'controls'):
    """Compare two direct pipelines with the fixed Nam-L3/backend-L1 pipeline."""
    if seed != 0:
        raise ValueError('This controlled experiment uses seed 0.')
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    rows, records, snapshots, logical_metrics = [], [], {}, {}
    for name, item in inputs.items():
        source = item['circuit']
        hybrid = transpile(source, basis_gates=BASIS, optimization_level=3,
                           seed_transpiler=0, approximation_degree=1.0)
        _equivalent(source, hybrid)
        logical_metrics[name] = metrics(hybrid)
        for backend_name in backends:
            backend = BACKENDS[backend_name]()
            snapshots[backend_name] = {'configuration': backend.configuration().to_dict(),
                                      'properties': backend.properties().to_dict()}
            workflows = [('Direct L1', source, 1, 'direct_L1'),
                         ('Direct L3', source, 3, 'direct_L3'),
                         ('Nam L3 → backend L1', hybrid, 1, 'nam_L3_backend_L1')]
            for label, circuit, level, slug in workflows:
                start = time.perf_counter()
                physical = transpile(circuit, backend=backend, optimization_level=level,
                                     initial_layout=list(range(5)), routing_method='sabre',
                                     seed_transpiler=seed, approximation_degree=1.0)
                seconds = time.perf_counter() - start
                _equivalent(source, physical, physical=True)
                path = output / 'compiled' / f'{name}_{slug}_{backend_name}_seed0.qpy'
                path.parent.mkdir(parents=True, exist_ok=True)
                _save(physical, path)
                text = qasm2.dumps(physical)
                if 'sx' in physical.count_ops() and 'gate sx ' not in text:
                    text = text.replace('include "qelib1.inc";',
                        'include "qelib1.inc";\ngate sx a { h a; s a; h a; }', 1)
                qasm_path = path.with_suffix('.qasm')
                qasm_path.write_text(text)
                if not Operator(physical).equiv(Operator(qasm2.load(qasm_path)), rtol=0, atol=1e-6):
                    raise ValueError(f'QASM round-trip failed: {qasm_path}')
                m = metrics(physical)
                rows.append({'Circuit': name, 'Backend': backend_name, 'Workflow': label,
                             'Gates': m['gates'], '2Q': m['2Q'], 'Depth': m['depth']})
                records.append({'circuit': name, 'backend': backend_name, 'workflow': label,
                                'backend_level': level, 'seed': seed, **m,
                                'compile_seconds': seconds,
                                'duration_ns': physical.estimate_duration(backend.target, unit='s') * 1e9,
                                'equivalent': True, 'qasm_equivalent': True,
                                'file': os.path.relpath(path, ROOT),
                                'qasm_file': os.path.relpath(qasm_path, ROOT)})
    table = pd.DataFrame(rows).sort_values(['Circuit', 'Backend', 'Workflow']).reset_index(drop=True)
    table.to_csv(output / 'results.csv', index=False)
    _json(output / 'metadata.json', {
        'versions': {p: importlib.metadata.version(p) for p in ('qiskit', 'qiskit-ibm-runtime')},
        'inputs': {n: {'sha256': v['sha256'], 'path': str(v['path'])} for n,v in inputs.items()},
        'hybrid_logical_metrics': logical_metrics, 'backends': snapshots,
        'policy': {'seed': seed, 'logical_seed': 0, 'initial_layout': list(range(5)),
                   'routing_method': 'sabre', 'approximation_degree': 1.0,
                   'hybrid_basis': BASIS, 'hybrid_logical_level': 3, 'hybrid_backend_level': 1},
        'runs': records,
        'notes': 'All table metrics are final compiled metrics. Direct L3 changes physical optimization effort; it is not a routing-only ablation. Inputs are already in Nam; hybrid performs additional logical level-3 optimization.'})
    return table
