# Social copy — v0.3.0-alpha.1 (the cognitive runtime)

Blog: https://blog.devart.solutions/blog/evomesh-0-3-the-cognitive-runtime
Release: https://github.com/Dev-Art-Solutions/EvoMesh/releases/tag/v0.3.0-alpha.1
Docs: https://evomesh.devart.solutions/#bdi

## X / Twitter

EvoMesh 0.3: agents that stop paying the model for what they already know. Plans that worked become procedures, reused with no planning call. Self-improvement starts from evidence and ends with a measurement: a landed fix is "verified" only when the problem stays gone.

https://blog.devart.solutions/blog/evomesh-0-3-the-cognitive-runtime

## Facebook / LinkedIn

**EvoMesh 0.3: the model is called for what the mesh does not already know.**

EvoMesh is a local-first multi-agent runtime that improves its own code. Its agents have always been real BDI agents: they commit to a plan and reconsider only on evidence. What they lacked was everything a cognitive runtime needs around that loop. This release adds it.

Goals now have dependencies, success predicates and retry budgets. A small rule engine runs every cycle, fed by events from the rest of the mesh, and never calls a model. Every plan the model writes is kept, and once the same plan has worked three times for the same goal, the agent reuses it with no planning call. We checked that on the live mesh: the goal came back and the planning counter did not move. Agents also hand each other structured work that comes back as a result. When one gets stuck, the Guardian diagnoses it without a model.

The part we cared about most is self-improvement. The agent that changes EvoMesh's own code no longer picks targets by rotation. Every change starts from evidence: a logged error, a failing test, an item a human wrote, or a failure that keeps coming back. Changes are ranked and worked one at a time, each with a budget. A separate reviewer and the test suite must both agree before anything lands. After it lands, the fix is watched, and only when the problem stays gone is it marked verified. This afternoon one went all the way round on its own.

Running it live also found four real bugs that the tests had not. One piled a new task onto a helper agent every cycle. Another undid a human's "drop this goal" when a long model call finished. All four are fixed in this release.

https://blog.devart.solutions/blog/evomesh-0-3-the-cognitive-runtime
