from algorithm.knowledge_extract.knowledge_space import store_extraction_to_neo4j


class FakeNeo4jClient:
    def __init__(self):
        self.nodes: dict[str, dict] = {}
        self.commands: list[tuple[str, dict]] = []
        self.closed = False

    def run_single(self, query, params):
        if "RETURN n" in query and params and "uid" in params:
            return self.nodes.get(params["uid"])
        return None

    def run(self, query, params):
        self.commands.append((query, params))
        if query.startswith("CREATE (n:"):
            self.nodes[params["uid"]] = {"uid": params["uid"]}
        elif " SET " in query and params.get("uid") in self.nodes:
            self.nodes[params["uid"]].update(params)
        return None

    def close(self):
        self.closed = True


def test_store_extraction_to_neo4j_discards_relationships_missing_nodes():
    client = FakeNeo4jClient()

    result = store_extraction_to_neo4j(
        graph={
            "nodes": [{"uid": "Term::a", "label": "Term", "properties": {"name": "A"}}],
            "relationships": [{"from_uid": "Term::a", "to_uid": "Term::missing", "type": "RELATED"}],
        },
        neo4j_client=client,
    )

    assert result["success"] is True
    assert result["nodes_created"] == 1
    assert result["nodes_merged"] == 0
    assert result["relations_created"] == 0
    assert all("MERGE (a)-" not in query for query, _params in client.commands)


def test_store_extraction_to_neo4j_does_not_persist_empty_name_on_merge():
    client = FakeNeo4jClient()
    client.nodes["Term::a"] = {"uid": "Term::a"}

    result = store_extraction_to_neo4j(
        graph={
            "nodes": [{"uid": "Term::a", "label": "Term", "properties": {"name": "", "description": "updated"}}],
            "relationships": [],
        },
        neo4j_client=client,
    )

    assert result["success"] is True
    assert result["nodes_created"] == 0
    assert result["nodes_merged"] == 1
    assert any(params.get("p_description") == "updated" for _query, params in client.commands)
    assert all("p_name" not in params for _query, params in client.commands)
