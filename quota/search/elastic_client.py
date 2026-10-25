"""Clients for the Elastic project (ELASTIC_TASKS E0). `es()`, `kibana()` and `find_reranker()` follow Elastic's
Cal Hacks starter (jdarmada/calhacks-paper-scout, common.py). Settings come from the git-ignored `.env`:
ELASTICSEARCH_URL, ELASTIC_API_KEY and optionally KIBANA_URL (default: the ES URL with ".es." swapped for ".kb.",
port kept). Nothing here runs unless the Elastic extra is installed and the keys are set."""

import os

EMBEDDING_ID = ".jina-embeddings-v5-text-small"


def _settings() -> tuple[str, str, str]:
    from quota.env import load_env
    load_env()
    es_url = os.environ.get("ELASTICSEARCH_URL", "").rstrip("/")
    kibana_url = (os.environ.get("KIBANA_URL") or es_url.replace(".es.", ".kb.", 1)).rstrip("/")
    if kibana_url == es_url:
        kibana_url = ""          # not an Elastic Cloud URL, so it cannot be guessed
    return es_url, kibana_url, os.environ.get("ELASTIC_API_KEY", "")


def configured() -> bool:
    """Keys are set and Elastic is not switched off (the test suite sets QUOTA_ELASTIC_OFF unless it is asked
    for live tests with QUOTA_ELASTIC_LIVE=1)."""
    es_url, _, key = _settings()
    return bool(es_url and key) and not os.environ.get("QUOTA_ELASTIC_OFF")


def es():
    from elasticsearch import Elasticsearch
    es_url, _, key = _settings()
    if not es_url:
        raise RuntimeError("ELASTICSEARCH_URL is not set in .env")
    return Elasticsearch(es_url, api_key=key, request_timeout=120) if key else Elasticsearch(es_url, request_timeout=120)


def kibana(method: str, path: str, **kwargs):
    """Call a Kibana API (Agent Builder, Workflows) with the same API key."""
    import requests
    _, kibana_url, key = _settings()
    if not kibana_url:
        raise RuntimeError("KIBANA_URL is not set in .env and cannot be derived from ELASTICSEARCH_URL")
    headers = {"Authorization": f"ApiKey {key}", "kbn-xsrf": "true", "Content-Type": "application/json",
               "x-elastic-internal-origin": "Kibana"}
    return requests.request(method, f"{kibana_url}{path}", headers=headers, timeout=300, **kwargs)


def find_reranker(client) -> str | None:
    """A rerank endpoint if the project has one (the newest Jina text reranker first)."""
    if os.environ.get("RERANK_INFERENCE_ID"):
        return os.environ["RERANK_INFERENCE_ID"]
    try:
        endpoints = client.inference.get(task_type="rerank").get("endpoints", [])
    except Exception:
        return None
    ids = [e["inference_id"] for e in endpoints]
    jina = sorted((i for i in ids if "jina" in i and "m0" not in i), reverse=True)
    return (jina or ids or [None])[0]


def embedding_id(client) -> str | None:
    """The Jina embeddings endpoint when the project has it, else None (the cluster's default model)."""
    try:
        client.inference.get(inference_id=EMBEDDING_ID)
        return EMBEDDING_ID
    except Exception:
        return None


def ping() -> dict:
    """Each check on its own: an API key without the cluster `monitor` privilege cannot read the cluster info,
    but can still search, embed and talk to Agent Builder."""
    client = es()
    try:
        info = client.info()
        cluster, version = info.get("cluster_name"), info.get("version", {}).get("number")
    except Exception as exc:
        cluster, version = f"(info not permitted: {type(exc).__name__})", "?"
    agents = kibana("GET", "/api/agent_builder/agents")
    return {"cluster": cluster, "version": version, "embeddings": embedding_id(client),
            "reranker": find_reranker(client), "agent_builder": agents.status_code}
