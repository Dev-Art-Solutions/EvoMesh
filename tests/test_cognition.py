from evomesh.cognition import AgentCycleTrace


def test_agent_cycle_trace_carries_agent_id():
    trace = AgentCycleTrace(agent_id="agent-1", turn=3, cognition="think", outcome="ok")
    assert trace.agent_id == "agent-1"
