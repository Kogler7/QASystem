import os
import sys

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

_mcp_vendor = os.path.join(_project_root, "algorithm", "mcp_servers", "_vendor")
if os.path.isdir(_mcp_vendor) and _mcp_vendor not in sys.path:
    sys.path.insert(0, _mcp_vendor)

_local_lib = os.path.join(_project_root, ".local")
if os.path.isdir(_local_lib) and _local_lib not in sys.path:
    sys.path.insert(0, _local_lib)

from algorithm.ans.QA.config import Config

_config = Config.load()


def get_config() -> Config:
    return _config


def get_neo4j_client():
    from algorithm.ans.QA.neo4j_client import MultiServerWikidataQueryClient
    cfg = get_config()
    return MultiServerWikidataQueryClient(
        neo4j_uri=cfg.NEO4J_URI,
        username=cfg.NEO4J_USER,
        password=cfg.NEO4J_PASSWORD,
    )


def get_entity_linker():
    from algorithm.ans.QA.entity_linker import EntityLinker
    client = get_neo4j_client()
    cfg = get_config()
    return EntityLinker(client, cfg)
