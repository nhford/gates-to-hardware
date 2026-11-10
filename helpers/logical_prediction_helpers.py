"""Five-qubit logical prediction experiment and notebook chart."""

import hashlib
import json
import math
from html import escape

import pandas as pd
from IPython.display import HTML, display
from qiskit import QuantumCircuit, qasm2, transpile
from qiskit.circuit.library import PermutationGate
from qiskit.transpiler import PassManager, generate_preset_pass_manager
from qiskit.transpiler.basepasses import AnalysisPass

from .comparison import BACKENDS, BASIS, _equivalent, metrics, optimize


class CountRoutingSwaps(AnalysisPass):
    def run(self, dag):
        self.property_set["inserted_swaps"] = dag.count_ops().get("swap", 0)


def calibration_fidelity(circuit, target):
    log_factors = []
    for instruction in circuit.data:
        name = instruction.operation.name
        qubits = tuple(circuit.find_bit(qubit).index for qubit in instruction.qubits)
        properties = target[name].get(qubits) if name in target else None
        error = properties.error if properties is not None else None
        if error is None or not math.isfinite(error) or not 0 <= error <= 1:
            return None
        log_factors.append(math.log1p(-error) if error < 1 else -math.inf)
    return math.exp(math.fsum(log_factors))


def compile_and_measure(circuit, source, target, seed):
    pm = generate_preset_pass_manager(
        optimization_level=1, target=target, initial_layout=list(range(source.num_qubits)),
        routing_method="sabre", seed_transpiler=seed, approximation_degree=1.0,
    )
    pm.post_routing = PassManager([CountRoutingSwaps()])
    physical = pm.run(circuit)
    _equivalent(source, physical, physical=True)
    return physical, {
        **metrics(physical),
        "duration_ns": physical.estimate_duration(target, unit="s") * 1e9,
        "fidelity": calibration_fidelity(physical, target),
        "swaps": pm.property_set["inserted_swaps"],
    }


def flatten_point(point):
    return {
        **{key: point[key] for key in ("circuit", "backend", "optimizer", "seed")},
        **{f"{stage}_{metric}": value
           for stage in ("logical_before", "logical_after", "compiled_before", "compiled_after")
           for metric, value in point[stage].items()},
    }


def load_suite(circuits, suite, *, min_width, max_width):
    inputs = {}
    for name in circuits:
        path = suite / f"{name}.qasm"
        source = qasm2.load(path, custom_instructions=qasm2.LEGACY_CUSTOM_INSTRUCTIONS)
        if not min_width <= source.num_qubits <= max_width or set(source.count_ops()) - set(BASIS):
            raise ValueError(f"{name} must be a {min_width}–{max_width}-qubit circuit in the Nam basis")
        inputs[name] = {
            "path": path, "circuit": source,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    return inputs


def optimize_suite(inputs, *, voqc_home, output, quartz_optimize=None):
    optimizers = ("VOQC", "Qiskit", "Quartz") if quartz_optimize else ("VOQC", "Qiskit")
    optimized = {}
    excluded = []
    for name, item in inputs.items():
        for optimizer in optimizers:
            try:
                if optimizer == "VOQC":
                    optimized.update(optimize({name: item}, voqc_home=voqc_home, qiskit_level=3,
                                              output=output, optimizers=("VOQC",)))
                elif optimizer == "Qiskit":
                    logical = transpile(item["circuit"], basis_gates=BASIS, optimization_level=3,
                                        seed_transpiler=0, approximation_degree=1.0)
                    if logical.layout is not None:
                        # Restore logical wire order before the fixed-layout backend compile.
                        permutation = logical.layout.final_index_layout()
                        restored = QuantumCircuit(logical.num_qubits)
                        restored.compose(logical, inplace=True)
                        if permutation != list(range(logical.num_qubits)):
                            restored.append(PermutationGate(permutation), restored.qubits)
                            restored = transpile(restored, basis_gates=BASIS, optimization_level=0)
                        logical = restored
                    _equivalent(item["circuit"], logical)
                    optimized[name, optimizer] = {"circuit": logical}
                else:
                    logical = quartz_optimize(item["circuit"])
                    if set(logical.count_ops()) - set(BASIS):
                        raise ValueError(f"{name}: Quartz output is outside the Nam basis")
                    _equivalent(item["circuit"], logical)
                    optimized[name, optimizer] = {"circuit": logical}
            except ValueError as error:
                if str(error) != "Circuit failed full-unitary equivalence at tolerance 1e-6.":
                    raise
                excluded.append(f"{name} / {optimizer}")

    if excluded:
        print("Excluded after failing logical equivalence:", ", ".join(excluded))
    return optimized, excluded


def run_pilot(circuits, backends, *, suite, voqc_home, output, seed=0):
    """Compile each source and verified logical candidate under a shared backend policy."""
    inputs = load_suite(circuits, suite, min_width=5, max_width=5)
    optimized, _ = optimize_suite(inputs, voqc_home=voqc_home, output=output)

    compiled_circuits = {}
    points = []
    for backend_name in backends:
        backend = BACKENDS[backend_name]()
        for name, item in inputs.items():
            source = item["circuit"]
            physical, baseline = compile_and_measure(source, source, backend.target, seed)
            compiled_circuits[name, backend_name, "Baseline"] = physical
            logical_baseline = metrics(source)
            for optimizer in ("VOQC", "Qiskit"):
                if (name, optimizer) not in optimized:
                    continue
                logical = optimized[name, optimizer]["circuit"]
                physical, candidate = compile_and_measure(logical, source, backend.target, seed)
                compiled_circuits[name, backend_name, optimizer] = physical
                points.append({
                    "circuit": name, "backend": backend_name, "optimizer": optimizer,
                    "seed": seed, "logical_before": logical_baseline, "logical_after": metrics(logical),
                    "compiled_before": baseline, "compiled_after": candidate,
                })

    results = pd.DataFrame(flatten_point(point) for point in points)
    results.to_csv(output / "paired_metrics.csv", index=False)
    print(f"{len(points)} paired comparisons across {len(inputs)} distinct inputs")
    print(f"Fidelity proxy available for {results[['compiled_before_fidelity', 'compiled_after_fidelity']].notna().all(axis=1).sum()} comparisons")
    return points, results, compiled_circuits


CHART = '''<!doctype html>
<meta charset="utf-8">
<style>
:root { color-scheme: light dark; }
body { font: 14px system-ui; margin: 12px; color: CanvasText; background: Canvas; }
h2 { margin: 0 0 12px; font-size: 20px; }
.controls { display: flex; flex-wrap: wrap; gap: 10px; }
label { display: grid; gap: 4px; font-size: 12px; }
select { font: inherit; padding: 5px; max-width: 240px; }
svg { width: 100%; max-width: 1000px; display: block; }
#detail { min-height: 36px; }
</style>
<h2>Logical savings vs compiled outcomes · superconducting</h2>
<div class="controls" id="controls"></div>
<p id="summary" role="status"></p>
<svg id="plot" viewBox="0 0 820 420" role="img" aria-label="Scatter plot of logical and compiled savings"></svg>
<p id="legend"></p>
<p id="detail">Hover over or focus a point to inspect its baseline and optimized values.</p>
<script>
const points = __POINTS__;
const axes = {
  x: [["2Q","Logical 2Q gates saved"],["gates","Logical total gates saved"],["depth","Logical depth layers saved"]],
  y: [["2Q","Compiled native 2Q gates saved"],["gates","Compiled total gates saved"],
      ["depth","Compiled depth layers saved"],["duration_ns","Estimated duration saved (ns)"],
      ["fidelity","Calibration-based fidelity proxy gain (pp)"],["swaps","Inserted SWAPs saved"]]
};
const optimizerColors = {VOQC:"#3182bd", Qiskit:"#d1762b", Quartz:"#629c48"};
const legend = document.getElementById("legend");
for (const name of [...new Set(points.map(point => point.optimizer))]) {
  const symbol = document.createElement("span");
  symbol.style.color = optimizerColors[name];
  symbol.textContent = "● ";
  legend.append(symbol, name+"   ");
}
const filterKeys = ["backend", "circuit", "optimizer"];
const controls = document.getElementById("controls");
const plot = document.getElementById("plot");
const summary = document.getElementById("summary");
const detail = document.getElementById("detail");
const selects = {};
function select(key, label, options) {
  const wrapper = document.createElement("label");
  wrapper.append(label);
  const menu = document.createElement("select");
  options.forEach(([value, text]) => menu.add(new Option(text, value)));
  menu.addEventListener("change", draw);
  wrapper.append(menu);
  controls.append(wrapper);
  selects[key] = menu;
}
select("x","X axis",axes.x);
select("y","Y axis",axes.y);
for (const key of filterKeys) {
  const names = [...new Set(points.map(point => point[key]))].sort();
  select(key,key[0].toUpperCase()+key.slice(1),[["","All"], ...names.map(name => [name,name])]);
}
function ranks(values) {
  const order = values.map((value,index) => ({value,index})).sort((a,b) => a.value-b.value);
  const result = [];
  for (let first=0; first<order.length;) {
    let end=first+1;
    while (end<order.length && order[end].value===order[first].value) end++;
    for (let j=first;j<end;j++) result[order[j].index]=(first+end-1)/2;
    first=end;
  }
  return result;
}
function spearman(data) {
  const xs=ranks(data.map(d=>d.x)), ys=ranks(data.map(d=>d.y));
  const center=(data.length-1)/2;
  let xx=0, yy=0, xy=0;
  xs.forEach((x,i)=>{const a=x-center,b=ys[i]-center; xx+=a*a; yy+=b*b; xy+=a*b;});
  return xx && yy ? (xy/Math.sqrt(xx*yy)).toFixed(2) : "undefined";
}
function element(tag, attrs, text) {
  const node=document.createElementNS("http://www.w3.org/2000/svg",tag);
  for (const [key,value] of Object.entries(attrs)) node.setAttribute(key,value);
  if (text!==undefined) node.textContent=text;
  plot.append(node);
  return node;
}
const format = value => Number(value.toPrecision(4)).toLocaleString("en-US");
function draw() {
  const xKey=selects.x.value, yKey=selects.y.value;
  const selected=points.filter(point=>filterKeys.every(key=>!selects[key].value || point[key]===selects[key].value));
  const data=selected.map(point=>{
    const xb=point.logical_before[xKey], xa=point.logical_after[xKey];
    const yb=point.compiled_before[yKey], ya=point.compiled_after[yKey];
    return {...point, xb, xa, yb, ya, x:xb-xa,
      y:point.gains ? (point.gains[yKey] ?? NaN) :
        ya===null || yb===null ? NaN : yKey==="fidelity" ? 100*(ya-yb) : yb-ya};
  }).filter(point=>Number.isFinite(point.y));
  plot.replaceChildren();
  detail.textContent="Hover over or focus a point to inspect its baseline and optimized values.";
  const reversals=data.filter(d=>d.x*d.y<0).length;
  summary.textContent=data.length
    ? data.length+" points · "+new Set(data.map(d=>d.circuit)).size+" circuits · Spearman ρ "+spearman(data)+" · "+reversals+" reversals"
    : "No plottable comparisons for this selection.";
  if (data.length < selected.length) summary.textContent += " · "+(selected.length-data.length)+" unavailable";
  if (!data.length) return;
  function integerAxis(key) {
    const values = data.map(point => point[key]);
    const min = Math.min(0, ...values), max = Math.max(0, ...values);
    const idealStep = Math.max(1, (max - min) / 5);
    const magnitude = 10 ** Math.floor(Math.log10(idealStep));
    const step = [1, 2, 5, 10].map(unit => unit * magnitude)
      .find(candidate => candidate >= idealStep);
    const first = Math.floor(min / step), last = Math.ceil(max / step);
    return {
      min: (first - 0.25) * step,
      max: (last + 0.25) * step,
      ticks: Array.from({length: last - first + 1}, (_, index) => (first + index) * step)
    };
  }
  const xAxis = integerAxis("x"), yAxis = integerAxis("y");
  const {min: xl, max: xh} = xAxis, {min: yl, max: yh} = yAxis;
  const x=v=>104+690*(v-xl)/(xh-xl), y=v=>352-328*(v-yl)/(yh-yl);
  function line(x1,y1,x2,y2,opacity) {element("line",{x1,y1,x2,y2,stroke:"currentColor","stroke-opacity":opacity});}
  for (const tick of xAxis.ticks) {
    line(x(tick), 24, x(tick), 352, .14);
    element("text", {x:x(tick),y:372,"text-anchor":"middle",fill:"currentColor","font-size":11}, tick.toLocaleString("en-US"));
  }
  for (const tick of yAxis.ticks) {
    line(104, y(tick), 794, y(tick), .14);
    element("text", {x:96,y:y(tick)+4,"text-anchor":"end",fill:"currentColor","font-size":11}, tick.toLocaleString("en-US"));
  }
  line(x(0),24,x(0),352,.6); line(104,y(0),794,y(0),.6);
  element("text",{x:449,y:405,"text-anchor":"middle",fill:"currentColor"},selects.x.selectedOptions[0].text);
  element("text",{transform:"translate(20 188) rotate(-90)","text-anchor":"middle",fill:"currentColor"},selects.y.selectedOptions[0].text);
  const groups = Map.groupBy ? Map.groupBy(data,d=>d.x+"|"+d.y) : data.reduce((map,d)=>{
    const key=d.x+"|"+d.y;
    map.set(key,[...(map.get(key)||[]),d]);
    return map;
  },new Map());
  for (const group of groups.values()) {
    const d=group[0];
    const message=group.map(p=>p.circuit+" / "+p.optimizer+" / "+p.backend+
      " · logical "+format(p.xb)+" → "+format(p.xa)+
      (p.gains ? " · median paired gain "+format(p.y)+
        " · seed range "+p.spread[yKey].map(format).join(" to ") :
        " · compiled "+format(p.yb)+" → "+format(p.ya))).join(" | ");
    const dot=element("circle",{cx:x(d.x),cy:y(d.y),r:group.length>1?7:5,tabindex:0,
      fill:group.some(p=>p.optimizer!==d.optimizer)?"currentColor":optimizerColors[d.optimizer],
      "fill-opacity":.82,"aria-label":message});
    const title=document.createElementNS("http://www.w3.org/2000/svg","title");
    title.textContent=message;
    dot.append(title);
    for (const event of ["mouseenter","focus"]) dot.addEventListener(event,()=>detail.textContent=message);
  }
}
draw();
</script>'''


def show_pilot_chart(points):
    """Display the interactive superconducting scatterplot."""
    payload = json.dumps(points, allow_nan=False).replace("<", "\\u003c")
    display(HTML(
        '<iframe title="Superconducting benchmark explorer" sandbox="allow-scripts" '
        'style="border:0;width:100%;height:690px" srcdoc="'
        + escape(CHART.replace("__POINTS__", payload), quote=True) + '"></iframe>'
    ))
