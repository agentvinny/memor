"""Embedding for semantic search (R3): a configurable embedder and lazy
vector cache.

Targets are embedded on first use and cached in the store's
`embeddings` table (kind, ref_id, dim, BLOB float32); search is numpy
cosine over the cached corpus — the corpus is small by design
(summaries + workspace-state bodies + user prompts, NOT raw tool
output), so no service is needed.

`get_embedder(cfg)` returns None (search falls back to FTS with a note)
when the provider is `off` or cannot be initialized (missing deps,
model, or GPU) — the semantic axis must never break the FTS path.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

from .config import Config


class EmbedError(RuntimeError):
    pass


class Embedder:
    """Protocol: embed(texts) -> list[list[float]] (L2-normalized)."""

    dim: int = 0

    def embed(self, texts: list[str]) -> list[list[float]]:  # pragma: no cover
        raise NotImplementedError


class LocalEmbedder(Embedder):
    """A sentence-transformer model on this machine. `device` is a GPU
    index (string) or "cpu"; the GPU is pinned via CUDA_VISIBLE_DEVICES
    before torch initializes CUDA."""

    def __init__(self, model: str, device: str = "1", batch: int = 32):
        if device and device != "cpu":
            os.environ.setdefault("CUDA_VISIBLE_DEVICES", str(device))
        import torch  # deferred: heavy import, only for this provider

        self._torch = torch
        import transformers
        self._tok = transformers.AutoTokenizer.from_pretrained(
            model, local_files_only=self._cached(model))
        self._model = self._load(model, transformers)
        self._model.eval()
        self._on_gpu = False
        if device != "cpu":
            if not torch.cuda.is_available():
                raise EmbedError(
                    f"cuda unavailable (device={device}, model={model})")
            self._on_gpu = True
            self._model.to("cuda:0")
        self._batch = batch
        # probe dim with one dummy vector
        self.dim = len(self.embed(["dim probe"])[0])

    @staticmethod
    def _cached(model: str) -> bool:
        from pathlib import Path
        return Path(os.path.expanduser(
            f"~/.cache/huggingface/hub/models--{model.replace('/', '--')}"
        )).exists()

    @staticmethod
    def _load(model: str, transformers) -> object:
        try:
            return transformers.AutoModel.from_pretrained(
                model, local_files_only=True)
        except Exception:
            # not in the local cache: one network load, then it's cached
            return transformers.AutoModel.from_pretrained(model)

    def embed(self, texts: list[str]) -> list[list[float]]:
        torch = self._torch
        dev = "cuda:0" if self._on_gpu else "cpu"
        out: list[list[float]] = []
        for i in range(0, len(texts), self._batch):
            chunk = [t[:2000] for t in texts[i:i + self._batch]]
            enc = self._tok(chunk, padding=True, truncation=True,
                            max_length=512, return_tensors="pt")
            with torch.no_grad():
                enc = {k: v.to(dev) for k, v in enc.items()}
                last = self._model(**enc).last_hidden_state
                mask = enc["attention_mask"].unsqueeze(-1).to(last.dtype)
                pooled = (last * mask).sum(1) / mask.sum(1).clamp(min=1)
                pooled = pooled / pooled.norm(
                    dim=-1, keepdim=True).clamp(min=1e-8)
                out.extend(pooled.float().cpu().tolist())
        return out


class ApiEmbedder(Embedder):
    """An OpenAI-compatible POST {base}/embeddings with body
    {"model", "input": [...]} (self-hosted or hosted endpoint)."""

    def __init__(self, base_url: str, api_key: str, model: str,
                 batch: int = 32):
        if not base_url:
            raise EmbedError("embed_api_base is required for the api provider")
        self._url = base_url.rstrip("/") + "/embeddings"
        self._key = api_key
        self._model = model
        self._batch = batch

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self._batch):
            chunk = texts[i:i + self._batch]
            body = json.dumps({"model": self._model,
                              "input": [t[:2000] for t in chunk]}).encode()
            req = urllib.request.Request(
                self._url, data=body,
                headers={"Content-Type": "application/json",
                         **({"Authorization": f"Bearer {self._key}"}
                            if self._key else {})})
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.loads(r.read())
            rows = sorted(data["data"], key=lambda d: d["index"])
            vecs = [d["embedding"] for d in rows]
            if not vecs:
                continue
            if self.dim and any(len(v) != self.dim for v in vecs):
                raise EmbedError("embedding dims inconsistent")
            self.dim = self.dim or len(vecs[0])
            out.extend(vecs)
        return out


def get_embedder(cfg: Config) -> Embedder | None:
    p = cfg.embed_provider
    if p == "off":
        return None
    try:
        if p == "local":
            return LocalEmbedder(cfg.embed_model, cfg.embed_device,
                                 cfg.embed_batch)
        if p == "api":
            return ApiEmbedder(cfg.embed_api_base, cfg.embed_api_key,
                               cfg.embed_model, cfg.embed_batch)
        raise EmbedError(f"unknown embed_provider: {p!r}")
    except Exception as e:  # noqa: BLE001 — the FTS path must survive
        print(f"memor: embedder unavailable ({p}): {e}", file=sys.stderr)
        return None
