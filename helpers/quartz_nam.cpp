// Run Quartz's native Nam preprocessing and bounded gate-count search.
#include <fstream>
#include <iostream>
#include "quartz/context/rule_parser.h"
#include "quartz/tasograph/substitution.h"
#include "quartz/tasograph/tasograph.h"

int main(int argc, char **argv) {
  using namespace quartz;
  if (argc != 7 && argc != 8) {
    std::cerr << "usage: quartz_nam INPUT ECC PREPROCESSED OUTPUT SECONDS HAS_CCZ [rigetti]\n";
    return 2;
  }
  ParamInfo params;
  Context nam({GateType::h, GateType::x, GateType::rz, GateType::cx,
               GateType::add, GateType::input_qubit, GateType::input_param}, &params);
  Context ccz({GateType::h, GateType::x, GateType::rz, GateType::cx, GateType::ccz,
               GateType::input_qubit, GateType::input_param}, &params);
  Context rigetti({GateType::rx1, GateType::rx3, GateType::x, GateType::rz, GateType::cz, GateType::add,
                   GateType::input_qubit, GateType::input_param}, &params);
  Context *basis = argc == 8 ? &rigetti : &nam;
  EquivalenceSet ecc;
  if (!ecc.load_json(basis, argv[2], false)) return 3;

  const bool has_ccz = std::string(argv[6]) == "1";
  auto graph = Graph::from_qasm_file(has_ccz ? &ccz : basis, argv[1]);
  if (!graph) return 4;
  auto combined = union_contexts(&ccz, &nam);
  if (has_ccz) {
    auto rules = RuleParser::ccz_cx_rz_rules();
    std::vector<Command> source_rules;
    std::vector<std::vector<Command>> destinations;
    if (rules.find_convert_commands(&nam, GateType::ccz, source_rules, destinations) != 2)
      return 5;
    // The upstream rule strings use full angles. This runtime stores RZ angles halved.
    for (auto &commands : destinations)
      for (auto &command : commands)
        if (command.tp == GateType::rz)
          for (auto &parameter : command.constant_params)
            parameter.second /= 2;
    std::unique_ptr<GraphXfer> first(GraphXfer::create_single_gate_GraphXfer(
        &ccz, &nam, &combined, source_rules[0], destinations[0]));
    std::unique_ptr<GraphXfer> second(GraphXfer::create_single_gate_GraphXfer(
        &ccz, &nam, &combined, source_rules[1], destinations[1]));
    graph = graph->toffoli_flip_greedy(GateType::rz, first.get(), second.get());
  }
  graph->rotation_merging(GateType::rz);
  std::ofstream(argv[3]) << graph->to_qasm(false, false, 16);
  auto result = graph->optimize(basis, ecc, argv[1], false, nullptr,
                               graph->gate_count() * 1.0001, std::stod(argv[5]));
  std::ofstream(argv[4]) << result->to_qasm(false, false, 16);
}
