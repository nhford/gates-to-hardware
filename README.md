# Does Gate Reduction Translate to Hardware?

Compare **VOQC and Qiskit** on `tof_3` and `qft_5` in the Nam gate set, then compile both outputs for saved IBM Manila and Lima backends. Open **comparison.ipynb** in this repo and run its sections. The first table compares the optimizers in eight rows; the final table compares Qiskit workflows in twelve rows. Both are sortable and filterable.

## Run

Requires **Python 3.11 or newer**, a C toolchain, and the native VOQC library described below.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m ipykernel install --user --name gate-comparison --display-name "Gate comparison"
```

Select the **Gate comparison** kernel in your notebook editor. If using another kernel, run `%pip install -r requirements.txt` in it and restart the kernel. No IBM account or Java is needed.

## VOQC setup

The notebook uses a built PyVOQC library in the neighboring `../pyvoqc/` folder. The local copy is set up for this Mac. `VOQC_HOME` is its only location setting. The helper runs it in an isolated Python subprocess; PyVOQC itself only needs the standard library, so it does not need to be pip-installed separately in every notebook kernel.

For another machine, follow [the pinned VOQC build instructions](docs/voqc-setup.md). The exact source revisions and OCaml dependency versions are recorded in [voqc-build.json](docs/voqc-build.json), and the required packaging correction is included as [mlvoqc-dune.patch](docs/mlvoqc-dune.patch). Everything needed to identify and rebuild the external optimizer is recorded in this repo; the platform-specific binary stays outside it.

## Comparison policy

- **Input:** identical five-qubit QASM from `benchmarks/nam_rz/`, using `h/x/rz/cx`.
- **Logical optimization:** VOQC's `optimize_nam()` pipeline; Qiskit level 3 with seed 0 in the same basis. No hardware placement or routing is used at this stage. VOQC runs a terminating pass sequence, with no search time budget or optimizer seed.
- **Normalization:** VOQC's rational Z rotations are exported as numerical rotations and any remaining output instructions are translated into the Nam basis using `BasisTranslator` only. No additional Qiskit optimization runs on VOQC's output. Save the raw VOQC output as well as the normalized result.
- **Physical compilation:** identical Qiskit level 1, SABRE routing, fixed initial placement `[0,1,2,3,4]`, seed 0 only. Manila and Lima both use `rz/sx/x/cx` but have different connectivity. This compilation seed is independent of logical optimization.
- **Validation:** compare complete source and output unitaries after each conversion and compilation, allowing global phase, accounting for physical layout, with absolute tolerance 1e-6. This pilot requires five-qubit inputs and five-qubit backends.
- **Summary:** one seed-0 compilation per circuit/optimizer/backend. All compiled metrics come from that run.

Logical gate counts are measured in the Nam basis; compiled counts are measured in the native IBM basis. Their raw totals are not directly comparable across these two vocabularies. Compare optimizers within each stage. Saved snapshots are historical; no hardware jobs or noisy simulations are run. Depth is unweighted circuit depth and estimated duration is not measured fidelity.

The input summary appears once. The final table has circuit, optimizer, backend, then gates/2Q/depth for both stages. Logical columns repeat across the two backend rows. Both result tables use [ITables](https://itables.org/) for clickable column sorting, global search, and per-column filters. Its default order is Circuit → Optimizer → Backend. Shared circuit/optimizer labels and logical metrics are visually merged across adjacent backend rows. Merging is recomputed on every sort or filter; interleaved groups stay unmerged. CSV exports retain complete values in every row. JavaScript must be enabled in the notebook viewer; trust the notebook if your editor requests it. Table assets are embedded for offline use, and the default rows are also saved in `outputs/results.csv`.

Two circuits demonstrate the pipeline, not a broad benchmark study. The first table compares the logical optimizers. The control table below supplies an original-input baseline compiled directly at backend level 1, plus a direct level-3 baseline. Use the direct level-1 result to assess whether prior logical optimization improves the final compiled result.

## Files

- `comparison.ipynb`: concise experiment and sortable table.
- `logical_prediction.ipynb`: pilot chart, Aer fidelity check, and expanded benchmark.
- `helpers/`: shared optimizer, compilation, and chart code used by both notebooks.
- `docs/`: pinned VOQC source/dependency versions, packaging patch, and native build instructions.
- `benchmarks/`: curated circuits, including all 30 Nam RZ inputs used by the prediction notebook.
- `guoq-benchmarks/`: optional, ignored copy of the full upstream benchmark collection for reference.
- `outputs/`: ignored cached `logical.qasm`/`logical.qpy` pairs and VOQC intermediate QASM/logs, compiled `.qasm` and `.qpy` circuits, `results.csv`, seed-0 `trials.csv`, and `metadata.json` containing settings, hashes, timings, versions, and snapshots. Trial file paths are relative to the repo.

Set `FORCE_OPTIMIZE=True` for a fresh logical run. Each circuit/optimizer has one folder, for example `outputs/optimized/tof_3_VOQC/`. Its `metadata.json` records the input, adapter, dependency, and native-engine hashes; cached files are reused only when these settings match. A new run replaces that folder’s current result. Hardware compilation reruns when its cell runs. The notebook assumes the repo is its working directory.

`requirements.txt` pins direct notebook dependencies. `requirements-lock.txt` captures the validated local environment; use the direct requirements on another operating system.

Each QASM export is reloaded and checked against its QPY circuit on the physical wires. QASM stores the gate sequence, while QPY also preserves Qiskit layout metadata. `file` and `qasm_file` in `trials.csv` link to the two formats.

QASM exports include an explicit `sx` gate definition (`h; s; h`) because SX is outside the standard OpenQASM 2 `qelib1.inc` set. They can be reloaded without Qiskit-specific legacy parser options.

Each optimizer cache includes `logical.qasm`: the final common-Nam-basis circuit whose logical metrics appear in the table. It is checked against `logical.qpy` on export, including cache hits. VOQC input/output QASM files remain available as intermediate representations.

## Controlled Qiskit workflows

The notebook ends with a separate 12-row table: two circuits × two backends × three workflows. It compares direct backend level 1, direct backend level 3, and logical Nam level 3 followed by backend level 1. Seed 0, initial placement, SABRE routing, and exact synthesis are fixed. All six table columns refer to final compiled results; direct runs have no separate logical output. The input files are already in Nam, so the hybrid adds logical optimization rather than merely changing the input vocabulary.

Results and reproducibility metadata are in `outputs/controls/`. Each of the twelve compiled circuits is saved as both QASM and QPY and checked for equivalence. This table is separate from the VOQC comparison; direct level 3 is an end-to-end baseline with more downstream optimization effort, not an isolated routing comparison.

## Publish the study on GitHub Pages

The static study page lives in [docs/index.html](docs/index.html). Its chart and downloads use relative paths, so they work under a GitHub project URL. The published chart is a snapshot of the expanded benchmark. It does not require a Python environment or build step on GitHub.

1. Commit the site files in `docs/` and push them to `main`.
2. In the GitHub repository, open **Settings → Pages**.
3. Under **Build and deployment**, choose **Deploy from a branch**. Select **main** and **/docs**, then click **Save**.

GitHub shows the published URL on the Pages settings screen. To preview locally, run `python3 -m http.server 8000 --directory docs` and open `http://localhost:8000/`.
