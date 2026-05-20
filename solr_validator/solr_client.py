import queue
import threading
from typing import Dict, Iterator, List, Optional

import requests


class SolrClient:
    def __init__(self, base_url: str, timeout: int = 60):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._mode: Optional[str] = None
        self._session = requests.Session()

    def detect_mode(self) -> str:
        """Return 'std' for standalone or 'solrcloud' for SolrCloud."""
        if self._mode is None:
            resp = self._session.get(
                f"{self.base_url}/admin/info/system",
                params={"wt": "json"},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            self._mode = resp.json().get("mode", "std")
        return self._mode

    def get_schema_fields(self, index_name: str) -> List[Dict]:
        """Return declared (non-dynamic) field dicts from the schema."""
        resp = self._session.get(
            f"{self.base_url}/{index_name}/schema/fields",
            params={"wt": "json"},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json().get("fields", [])

    def get_dynamic_fields(self, index_name: str) -> List[Dict]:
        """Return dynamic field pattern dicts from the schema."""
        resp = self._session.get(
            f"{self.base_url}/{index_name}/schema/dynamicfields",
            params={"wt": "json"},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json().get("dynamicFields", [])

    def get_doc_count(self, index_name: str) -> int:
        resp = self._session.get(
            f"{self.base_url}/{index_name}/select",
            params={"q": "*:*", "rows": 0, "wt": "json"},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()["response"]["numFound"]

    def _batch_fetch_worker(
        self,
        index_name: str,
        fl: str,
        batch_size: int,
        resume_from_id: Optional[str],
        out_queue: queue.Queue,
        sentinel: object,
    ) -> None:
        params: Dict = {
            "q": "*:*",
            "sort": "id asc",
            "rows": batch_size,
            "fl": fl,
            "wt": "json",
            "cursorMark": "*",
        }
        if resume_from_id:
            # Exclusive lower bound: skip all docs with id <= resume_from_id
            params["fq"] = f"id:{{{resume_from_id} TO *}}"

        try:
            while True:
                resp = self._session.get(
                    f"{self.base_url}/{index_name}/select",
                    params=params,
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                data = resp.json()
                docs = data["response"]["docs"]
                next_cursor = data.get("nextCursorMark")
                out_queue.put(docs)
                if not docs or next_cursor == params["cursorMark"]:
                    break
                params["cursorMark"] = next_cursor
        except Exception as exc:
            out_queue.put(exc)
        finally:
            out_queue.put(sentinel)

    def stream_docs(
        self,
        index_name: str,
        field_list: List[str],
        batch_size: int = 1000,
        resume_from_id: Optional[str] = None,
    ) -> Iterator[Dict]:
        """Yield documents one at a time; fetching happens in a background thread."""
        fl = ",".join(field_list)
        sentinel = object()
        # Buffer up to 4 batches ahead
        out_queue: queue.Queue = queue.Queue(maxsize=4)
        thread = threading.Thread(
            target=self._batch_fetch_worker,
            args=(index_name, fl, batch_size, resume_from_id, out_queue, sentinel),
            daemon=True,
        )
        thread.start()
        while True:
            item = out_queue.get()
            if item is sentinel:
                break
            if isinstance(item, Exception):
                thread.join()
                raise item
            yield from item
        thread.join()

    # ── Admin API (used only by selftest) ──────────────────────────────────

    def delete_index(self, index_name: str) -> None:
        mode = self.detect_mode()
        if mode == "solrcloud":
            resp = self._session.get(
                f"{self.base_url}/admin/collections",
                params={"action": "DELETE", "name": index_name, "wt": "json"},
                timeout=self.timeout,
            )
        else:
            resp = self._session.get(
                f"{self.base_url}/admin/cores",
                params={"action": "UNLOAD", "core": index_name,
                        "deleteIndex": "true", "deleteInstanceDir": "true",
                        "wt": "json"},
                timeout=self.timeout,
            )
        # 404 is fine — index may not exist
        if resp.status_code not in (200, 404):
            resp.raise_for_status()

    def create_index(self, index_name: str, config_set: str = "_default") -> None:
        mode = self.detect_mode()
        if mode == "solrcloud":
            resp = self._session.get(
                f"{self.base_url}/admin/collections",
                params={
                    "action": "CREATE",
                    "name": index_name,
                    "numShards": 1,
                    "replicationFactor": 1,
                    "collection.configName": config_set,
                    "wt": "json",
                },
                timeout=self.timeout,
            )
        else:
            resp = self._session.get(
                f"{self.base_url}/admin/cores",
                params={
                    "action": "CREATE",
                    "name": index_name,
                    "configSet": config_set,
                    "wt": "json",
                },
                timeout=self.timeout,
            )
        resp.raise_for_status()

    def index_docs(self, index_name: str, docs: List[Dict], commit: bool = True) -> None:
        import json as _json
        params = {"commit": "true"} if commit else {}
        resp = self._session.post(
            f"{self.base_url}/{index_name}/update",
            params={**params, "wt": "json"},
            data=_json.dumps(docs),
            headers={"Content-Type": "application/json"},
            timeout=self.timeout,
        )
        resp.raise_for_status()

    def delete_doc(self, index_name: str, doc_id: str, commit: bool = True) -> None:
        import json as _json
        params = {"commit": "true"} if commit else {}
        resp = self._session.post(
            f"{self.base_url}/{index_name}/update",
            params={**params, "wt": "json"},
            data=_json.dumps({"delete": {"id": doc_id}}),
            headers={"Content-Type": "application/json"},
            timeout=self.timeout,
        )
        resp.raise_for_status()
