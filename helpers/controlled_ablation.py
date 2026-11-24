"""Measure individual Level 1 cleanup additions on the fixed reversal cohort."""
import json
from pathlib import Path
from statistics import median
from qiskit import qasm2
from qiskit.transpiler import PassManager
from qiskit.transpiler.passes import InverseCancellation, Optimize1qGatesDecomposition
from .comparison import _equivalent
from .controlled_compilation import controlled_compile, calibration_free_target
from .logical_prediction_helpers import optimize_suite
from .logical_prediction_v2 import EXPANDED_BACKENDS, _connected_target, IBM_NEW_BASIS


def run():
    root = Path(__file__).resolve().parents[1]
    cohort = json.loads((root / 'docs/controlled-compilation.json').read_text())['rows']
    cohort = [row for row in cohort if row['level1_reversal']]
    names = sorted({row['circuit'] for row in cohort})
    inputs = {name: {'circuit': qasm2.load(root / f'benchmarks/ibmnew/{name}.qasm',
              custom_instructions=qasm2.LEGACY_CUSTOM_INSTRUCTIONS)} for name in names}
    candidates, excluded = optimize_suite(inputs, voqc_home=None, output=None,
                                         basis=IBM_NEW_BASIS, optimizers=('Qiskit',))
    assert not excluded
    results = []
    for row in cohort:
        name, backend = row['circuit'], row['backend']
        source = inputs[name]['circuit']
        candidate = candidates[name, 'Qiskit']['circuit']
        target, _ = _connected_target(EXPANDED_BACKENDS[backend](), source.num_qubits)
        clean = calibration_free_target(target)
        seeds = []
        for seed in range(5):
            paths = []
            for circuit in (source, candidate):
                physical, values = controlled_compile(circuit, source, target, seed)
                counts = {'controlled': values['gates']}
                variants = {
                    'inverse_only': [InverseCancellation()],
                    'one_qubit_only': [Optimize1qGatesDecomposition(target=clean)],
                    'one_qubit_then_inverse': [Optimize1qGatesDecomposition(target=clean), InverseCancellation()],
                }
                for label, passes in variants.items():
                    output = PassManager(passes).run(physical)
                    _equivalent(physical, output)
                    output._layout = physical.layout
                    _equivalent(source, output, physical=True)
                    counts[label] = output.size()
                paths.append(counts)
            seeds.append({'seed': seed, 'before': paths[0], 'after': paths[1],
                          'gains': {key: paths[0][key] - paths[1][key] for key in paths[0]}})
        result = {'circuit': name, 'backend': backend, 'preset_level1_gain': row['level1_gate_savings'],
                  'gains': {key: median(seed['gains'][key] for seed in seeds) for key in seeds[0]['gains']},
                  'seeds': seeds}
        results.append(result)
        print(name, backend, result['gains'], flush=True)
    payload = {'policy': {'cohort': '30 IBM New Qiskit Level 1 reversals', 'seeds': list(range(5)),
               'placement_routing_translation': 'unchanged controlled_compile',
               'calibrations': 'removed', 'addition_position': 'after final BasisTranslator',
               'cleanup_repetitions': 1}, 'rows': results}
    (root / 'docs/controlled-ablation.json').write_text(json.dumps(payload, indent=2) + '\n')


if __name__ == '__main__':
    run()
