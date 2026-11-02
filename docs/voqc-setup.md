# Rebuild VOQC

The experiment uses the Python wrapper around VOQC's native OCaml library.
Source revisions and dependency versions are pinned in [voqc-build.json](voqc-build.json).
These are the sources used for the validated local experiment, not floating branches.

## Prerequisites

On macOS, install the Xcode Command Line Tools if needed, then use Homebrew:

```sh
xcode-select --install
brew install opam gmp pkgconf
```

On Linux, install the equivalent C toolchain, opam, GMP development headers, and
pkg-config using your package manager. Build for the same architecture as the
notebook's Python interpreter; do not copy a macOS binary to Linux or an ARM
binary into an Intel Python environment.

## Build from a fresh checkout

Start in this experiment's repository directory. These commands create neighboring
`pyvoqc/` and `mlvoqc/` checkouts and a separate opam switch. If those directories
already exist, use your existing checkouts at the pinned revisions rather than
cloning over them. The switch name below should be unused for first-time setup.

```sh
REPO_ROOT="$PWD"

git clone https://github.com/inQWIRE/pyvoqc.git ../pyvoqc
git -C ../pyvoqc checkout 72a399716e080d84532870e9357aec42c3782119

git clone https://github.com/inQWIRE/mlvoqc.git ../mlvoqc
git -C ../mlvoqc checkout 40548f3374880ac49b8f3d27181d0c2d727dbe58
git -C ../mlvoqc apply "$REPO_ROOT/docs/mlvoqc-dune.patch"

opam init --bare --no-setup
opam switch create voqc-comparison ocaml-base-compiler.4.14.2
eval "$(opam env --switch=voqc-comparison --set-switch)"

opam install -y dune.3.17.2 menhir.20240715 ocamlfind.1.9.6 openQASM.0.4.0 zarith.1.14
opam pin add -y voqc ../mlvoqc --kind=path

(
    cd ../pyvoqc
    dune build lib/libvoqc.so
    mkdir -p pyvoqc/lib
    cp _build/default/lib/libvoqc.so pyvoqc/lib/libvoqc.so
)
```

The pinned mlvoqc revision has malformed dependency declarations in `dune-project`.
The included patch fixes their grouping and quotes the version strings. It does
not change the optimizer or its extracted OCaml implementation. The old `mapping`
branch named in the upstream wrapper README is not used.

The resulting layout is:

```text
parent/
├── final-project/
│   ├── comparison.ipynb
│   └── docs/
├── pyvoqc/
│   └── pyvoqc/
│       ├── voqc.py
│       └── lib/libvoqc.so
└── mlvoqc/
```

The `.so` filename is also used on macOS. It is a compiled shared library, not a
Python package that pip can build without the OCaml toolchain. The shared library
links to GMP, which must remain installed.

## Verify

From the experiment repo, after installing its Python requirements, run:

```sh
.venv/bin/python - <<'PY'
from comparison import load_inputs, optimize

inputs = load_inputs(["tof_3"])
optimize(inputs, voqc_home="../pyvoqc", force=True)
PY
```

This runs both VOQC and Qiskit on the small input and checks their full-unitary
equivalence. It writes fresh optimizer outputs under the ignored `outputs/` folder.
Then open the notebook using that Python environment and run all cells. If the
wrapper is elsewhere, change `VOQC_HOME` in the notebook.
