import sys

sys.path.insert(0, "src/evomesh")
from _agent_ids import AgentId, is_valid, make_id
from phase_label import AgentPhase, phase_label
from verdict_label import verdict_label

print("AgentId:", AgentId("root.child"),
      AgentId("root.child").parent(),
      AgentId("root.child").child("g"))
print("is_valid:", is_valid("a.b_c"), is_valid("bad id"), is_valid(123))
print("make_id:", make_id("root", "", "child"))
print("phase:", phase_label(AgentPhase.THINKING),
      phase_label("thinking"), phase_label("unknown_phase"))
print("verdict:", verdict_label("passed"), verdict_label(None), verdict_label("FAILED"))
