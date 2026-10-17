# Why GraphSleuth: Graph > SQL/vector for agentic investigation

GraphSleuth treats FalkorDB as the agent's context layer. Instead of stitching tables (SQL) or fuzzy chunks (vector search), it follows typed relationships in a graph — perfect for questions like "how are these accounts connected?" or "who knows this system?".

Key points:
- Multi-hop queries (shared devices/beneficiaries, money flow) map naturally to Cypher traversals.
- Graph algorithms (WCC, label propagation, betweenness/harmonic, PageRank) are first-class agent tools — the agent decides when to call them.
- Per-tenant multigraphs: isolation is structural, not a WHERE filter.
- Memory & handoffs live as nodes/edges (Session/Message, MemoryNote, Handoff) — shared state between agents across sessions.
- Company brain: org questions need path reasoning (who worked on projects using a system, dependency closure, decision trace to source) — graph-native.
- Explainability: answer always includes Evidence + Graph path + Actions + node IDs.

Read WHY_GRAPH.md for a concrete comparison (recursive CTE vs Cypher).
