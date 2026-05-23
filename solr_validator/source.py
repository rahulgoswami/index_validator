import json
from abc import ABC, abstractmethod
from typing import Dict, Iterator, List, Optional

from .solr_client import SolrClient


class Source(ABC):
    @abstractmethod
    def iter_docs(self) -> Iterator[Dict]:
        pass

    @abstractmethod
    def get_doc_count(self) -> Optional[int]:
        pass

    @abstractmethod
    def label(self) -> str:
        pass


class IndexSource(Source):
    def __init__(
        self,
        client: SolrClient,
        index_name: str,
        field_list: List[str],
        batch_size: int = 1000,
        resume_from_id: Optional[str] = None,
    ):
        self._client = client
        self._index_name = index_name
        self._field_list = field_list
        self._batch_size = batch_size
        self._resume_from_id = resume_from_id

    def iter_docs(self) -> Iterator[Dict]:
        return self._client.stream_docs(
            self._index_name,
            self._field_list,
            self._batch_size,
            self._resume_from_id,
        )

    def get_doc_count(self) -> Optional[int]:
        return self._client.get_doc_count(self._index_name)

    def label(self) -> str:
        return f"index:{self._index_name}"


class FileSource(Source):
    def __init__(self, path: str, resume_from_id: Optional[str] = None):
        self._path = path
        self._resume_from_id = resume_from_id
        self._meta: Dict = {}
        self._meta_loaded: bool = False

    def _load_meta(self) -> None:
        """Peek at the first line of the file and cache __meta__ if present."""
        if self._meta_loaded:
            return
        self._meta_loaded = True
        try:
            with open(self._path) as fh:
                first_line = fh.readline()
        except OSError:
            return
        if not first_line:
            return
        try:
            first = json.loads(first_line)
        except json.JSONDecodeError:
            return
        if isinstance(first, dict) and "__meta__" in first:
            self._meta = first["__meta__"]

    def iter_docs(self) -> Iterator[Dict]:
        with open(self._path) as fh:
            first_line = fh.readline()
            if not first_line:
                return
            first = json.loads(first_line)
            if "__meta__" in first:
                self._meta = first["__meta__"]
                self._meta_loaded = True
            else:
                # No meta header; treat the first line as a document
                self._meta_loaded = True
                if not self._resume_from_id or first.get("id", "") > self._resume_from_id:
                    yield first

            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                doc = json.loads(raw)
                if self._resume_from_id and doc.get("id", "") <= self._resume_from_id:
                    continue
                yield doc

    def get_doc_count(self) -> Optional[int]:
        self._load_meta()
        return self._meta.get("doc_count")

    def label(self) -> str:
        return f"file:{self._path}"


def parse_source_spec(spec: str, client: SolrClient, field_list: List[str],
                      batch_size: int, resume_from_id: Optional[str]) -> Source:
    """Parse 'index:<name>' or 'file:<path>' into a Source instance."""
    if spec.startswith("index:"):
        return IndexSource(client, spec[6:], field_list, batch_size, resume_from_id)
    if spec.startswith("file:"):
        return FileSource(spec[5:], resume_from_id)
    raise ValueError(
        f"Source spec must start with 'index:' or 'file:' — got: {spec!r}"
    )
