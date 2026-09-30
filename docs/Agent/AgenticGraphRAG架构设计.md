# Agentic GraphRAG Architecture

## Boundary

This system treats Skill files as workflow policy and MCP tools as executable capabilities.

The orchestrator should not know domain business rules directly. It only knows:

- how to maintain state,
- how to call tools,
- how to normalize evidence,
- how to enforce final answer contracts.

## Core Contracts

- `GraphAnswerState`: mutable session state.
- `ProblemFrame`: normalized user question and routing metadata.
- `LinkedEntity`: graph node candidate.
- `GraphPath`: path or triple evidence from graph search.
- `EvidenceItem`: one atomic support unit.
- `EvidencePack`: ranked collection of support units.
- `FinalGraphAnswer`: API response and evaluation target.

## Skill Responsibilities

The `agentic-graphrag` Skill defines:

1. mandatory routing steps,
2. when graph search is enough,
3. when SQL or vector evidence is required,
4. evidence sufficiency rules,
5. final output format.

## MCP Responsibilities

MCP tools should stay small and typed:

- `locate_knowledge_space`
- `recognize_intent`
- `link_entities`
- `query_entity_properties`
- `search_graph`
- `retrieve_chunks`
- `generate_cypher`
- `execute_sql`
- `verify_evidence`

## Recommended First Integration

1. Wrap existing graph tools as `graph-search`.
2. Add `retrieve_chunks` by using node `VectorAddress`.
3. Add read-only Cypher execution with schema allowlist.
4. Enable `AGENTIC_GRAPHRAG_TOOL_CLIENT=mcp` to use the MCP-backed client.
5. Add streaming events for frontend trace display.
