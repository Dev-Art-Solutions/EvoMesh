from evomesh.cognition import AgentCycleTrace, CycleReply


def test_agent_cycle_trace_carries_agent_id():
    trace = AgentCycleTrace(agent_id="agent-1", turn=3, cognition="think", outcome="ok")
    assert trace.agent_id == "agent-1"


def test_cycle_reply_defaults_done_false():
    reply = CycleReply(step="deliberate", result="ok")
    assert reply.done is False
    assert "deliberate" in repr(reply)
