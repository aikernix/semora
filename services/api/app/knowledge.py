from __future__ import annotations

import json
import os
import re
from typing import Any
from urllib.parse import quote

import httpx
from semora_contracts import EvidenceRef, KnowledgeResearchResult, KnowledgeResearchStep


class KnowledgeCoreError(RuntimeError):
    pass


class KnowledgeCoreClient:
    provider = "semora-knowledge"

    def __init__(
        self,
        base_url: str | None = None,
        internal_token: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = (base_url or os.getenv("KNOWLEDGE_SERVICE_URL", "http://localhost:8003")).rstrip("/")
        self.internal_token = internal_token or os.getenv("INTERNAL_TOKEN", "semora-dev-internal")
        self._transport = transport

    async def _request(self, method: str, endpoint: str, **kwargs: Any) -> Any:
        try:
            async with httpx.AsyncClient(timeout=60, transport=self._transport) as client:
                response = await client.request(
                    method,
                    f"{self.base_url}/{endpoint.lstrip('/')}",
                    headers={"X-Semora-Internal-Token": self.internal_token},
                    **kwargs,
                )
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as exc:
            detail = ""
            try:
                detail = str(exc.response.json().get("detail", ""))
            except ValueError:
                pass
            raise KnowledgeCoreError(f"Knowledge Core HTTP {exc.response.status_code} {detail}".strip()) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise KnowledgeCoreError(f"Knowledge Core 请求失败：{type(exc).__name__}") from exc

    async def status(self) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=5, transport=self._transport) as client:
                response = await client.get(f"{self.base_url}/healthz")
                response.raise_for_status()
                return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise KnowledgeCoreError(f"Knowledge Core 健康检查失败：{type(exc).__name__}") from exc

    async def list_knowledge_bases(self) -> list[dict[str, Any]]:
        rows = await self._request("GET", "/internal/v1/knowledge-bases")
        return [
            {
                "id": row["id"],
                "name": row["name"],
                "description": row.get("description", ""),
                "documents": row.get("knowledge_count", 0),
                "chunks": row.get("chunk_count", 0),
                "status": "processing" if row.get("is_processing") else "ready",
                "provider": "semora-knowledge",
            }
            for row in rows
        ]

    async def create_knowledge_base(self, name: str, description: str) -> dict[str, Any]:
        return await self._request("POST", "/internal/v1/knowledge-bases", json={"name": name, "description": description})

    async def list_knowledge(self, kb_id: str, page: int, page_size: int) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"/internal/v1/knowledge-bases/{kb_id}/documents",
            params={"page": page, "page_size": page_size},
        )

    async def get_knowledge(self, document_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/internal/v1/documents/{document_id}")

    async def get_document_chunks(self, document_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/internal/v1/documents/{document_id}/chunks")

    async def retry_document(self, document_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/internal/v1/documents/{document_id}/retry")

    async def download_document(self, document_id: str) -> tuple[bytes, str, str, str]:
        try:
            async with httpx.AsyncClient(timeout=60, transport=self._transport) as client:
                response = await client.get(
                    f"{self.base_url}/internal/v1/documents/{document_id}/content",
                    headers={"X-Semora-Internal-Token": self.internal_token},
                )
                response.raise_for_status()
                return (
                    response.content,
                    response.headers.get("X-Semora-Filename", "document"),
                    response.headers.get("Content-Type", "application/octet-stream"),
                    response.headers.get("X-Semora-Content-Hash", ""),
                )
        except httpx.HTTPStatusError as exc:
            raise KnowledgeCoreError(f"Knowledge Core HTTP {exc.response.status_code}") from exc
        except httpx.HTTPError as exc:
            raise KnowledgeCoreError(f"Knowledge Core 请求失败：{type(exc).__name__}") from exc

    async def upload_document(self, kb_id: str, filename: str, content: bytes, content_type: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/internal/v1/knowledge-bases/{kb_id}/documents",
            files={"file": (filename, content, content_type or "application/octet-stream")},
        )

    async def search_evidence(self, query: str, kb_ids: list[str] | None = None) -> list[EvidenceRef]:
        payload = await self._request(
            "POST",
            "/internal/v1/search",
            json={"query": query, "knowledge_base_ids": kb_ids or [], "top_k": 8},
        )
        evidence = []
        for item in payload.get("results", []):
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            evidence.append(
                EvidenceRef(
                    knowledge_base_id=item["knowledge_base_id"],
                    document_id=item["knowledge_id"],
                    chunk_id=item["id"],
                    locator=item.get("locator") or f"分块 {item.get('chunk_index', 0)}",
                    revision=str(metadata.get("revision") or "current"),
                    title=item.get("title") or "知识文档",
                    summary=str(item.get("content") or "")[:1200],
                )
            )
        return evidence

    async def research(self, query: str, kb_ids: list[str] | None = None) -> KnowledgeResearchResult:
        evidence = await self.search_evidence(query, kb_ids)
        return KnowledgeResearchResult(
            provider=self.provider,
            agent_id="native-hybrid-search",
            session_id="",
            answer="",
            steps=[
                KnowledgeResearchStep(
                    kind="retrieval",
                    tool="hybrid_search",
                    summary=f"完成本地混合检索，获得 {len(evidence)} 条证据",
                )
            ],
            evidence=evidence,
        )


class WeKnoraClient:
    provider = "weknora"

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = (base_url or os.getenv("WEKNORA_BASE_URL", "")).rstrip("/")
        self.api_key = api_key or os.getenv("WEKNORA_API_KEY", "")
        self._transport = transport

    def _ensure_configured(self) -> None:
        if not self.base_url or not self.api_key:
            raise KnowledgeCoreError("WeKnora 未配置：需要 WEKNORA_BASE_URL 和 WEKNORA_API_KEY")

    @staticmethod
    def _unwrap(payload: Any) -> Any:
        if not isinstance(payload, dict):
            raise KnowledgeCoreError("WeKnora 返回了无效响应")
        if payload.get("success") is False:
            error = payload.get("error")
            if isinstance(error, dict):
                message = error.get("message") or error.get("code")
            else:
                message = payload.get("message") or error
            raise KnowledgeCoreError(f"WeKnora 请求失败：{message or 'unknown error'}")
        return payload.get("data") if "data" in payload else payload

    async def _request(self, method: str, endpoint: str, **kwargs: Any) -> Any:
        self._ensure_configured()
        headers = dict(kwargs.pop("headers", {}))
        headers.update({"X-API-Key": self.api_key, "Accept": "application/json"})
        try:
            async with httpx.AsyncClient(timeout=60, transport=self._transport) as client:
                response = await client.request(
                    method,
                    f"{self.base_url}/{endpoint.lstrip('/')}",
                    headers=headers,
                    **kwargs,
                )
                response.raise_for_status()
                payload = response.json()
                return self._unwrap(payload), payload
        except KnowledgeCoreError:
            raise
        except httpx.HTTPStatusError as exc:
            detail = ""
            try:
                body = exc.response.json()
                error = body.get("error") if isinstance(body, dict) else None
                if isinstance(error, dict):
                    detail = str(error.get("message") or error.get("code") or "")
                elif isinstance(body, dict):
                    detail = str(body.get("message") or "")
            except ValueError:
                pass
            raise KnowledgeCoreError(f"WeKnora HTTP {exc.response.status_code} {detail}".strip()) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise KnowledgeCoreError(f"WeKnora 请求失败：{type(exc).__name__}") from exc

    async def status(self) -> dict[str, Any]:
        rows = await self.list_knowledge_bases()
        return {
            "provider": self.provider,
            "status": "ready",
            "knowledge_bases": len(rows),
            "instance_url": self.base_url.removesuffix("/api/v1"),
            "agent_id": "semora-llm-wiki-researcher",
            "api_key_configured": bool(self.api_key),
        }

    async def list_knowledge_bases(self) -> list[dict[str, Any]]:
        data, _ = await self._request("GET", "/knowledge-bases")
        rows = data if isinstance(data, list) else []
        return [
            {
                "id": str(row.get("id", "")),
                "name": row.get("name") or "未命名知识库",
                "description": row.get("description") or "",
                "documents": row.get("knowledge_count", 0),
                "chunks": row.get("chunk_count", 0),
                "status": "processing" if row.get("is_processing") else "ready",
                "provider": self.provider,
            }
            for row in rows
            if isinstance(row, dict)
        ]

    async def create_knowledge_base(self, name: str, description: str) -> dict[str, Any]:
        data, _ = await self._request(
            "POST", "/knowledge-bases", json={"name": name, "description": description, "type": "document"}
        )
        return data if isinstance(data, dict) else {}

    async def list_knowledge(self, kb_id: str, page: int, page_size: int) -> dict[str, Any]:
        data, envelope = await self._request(
            "GET",
            f"/knowledge-bases/{kb_id}/knowledge",
            params={"page": page, "page_size": page_size},
        )
        return {
            "documents": data if isinstance(data, list) else [],
            "total": envelope.get("total", len(data) if isinstance(data, list) else 0),
            "page": envelope.get("page", page),
            "page_size": envelope.get("page_size", page_size),
        }

    async def get_knowledge(self, document_id: str) -> dict[str, Any]:
        data, _ = await self._request("GET", f"/knowledge/{document_id}")
        return data if isinstance(data, dict) else {}

    async def get_document_chunks(self, document_id: str) -> dict[str, Any]:
        document = await self.get_knowledge(document_id)
        data, _ = await self._request(
            "GET", f"/chunks/{document_id}", params={"page": 1, "page_size": 1000}
        )
        return {"document": document, "chunks": data if isinstance(data, list) else []}

    async def retry_document(self, document_id: str) -> dict[str, Any]:
        data, _ = await self._request("POST", f"/knowledge/{document_id}/reparse")
        return data if isinstance(data, dict) else {"document_id": document_id, "state": "queued"}

    async def download_document(self, document_id: str) -> tuple[bytes, str, str, str]:
        raise KnowledgeCoreError("WeKnora Adapter 暂不代理原文下载，请在 WeKnora 中查看原文")

    async def upload_document(self, kb_id: str, filename: str, content: bytes, content_type: str) -> dict[str, Any]:
        data, _ = await self._request(
            "POST",
            f"/knowledge-bases/{kb_id}/knowledge/file",
            files={"file": (filename, content, content_type or "application/octet-stream")},
            data={"enable_multimodel": "true"},
        )
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _map_evidence(rows: list[Any], selected_ids: list[str]) -> list[EvidenceRef]:
        evidence: list[EvidenceRef] = []
        seen: set[str] = set()
        for index, item in enumerate(rows):
            if not isinstance(item, dict):
                continue
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            chunk_index = int(item.get("chunk_index") or 0)
            chunk_id = str(item.get("id") or item.get("chunk_id") or f"weknora-result-{index}")
            if chunk_id in seen:
                continue
            seen.add(chunk_id)
            evidence.append(
                EvidenceRef(
                    knowledge_base_id=str(
                        item.get("knowledge_base_id")
                        or item.get("kb_id")
                        or (selected_ids[0] if len(selected_ids) == 1 else "weknora")
                    ),
                    document_id=str(item.get("knowledge_id") or item.get("document_id") or ""),
                    chunk_id=chunk_id,
                    locator=str(item.get("locator") or f"分块 {chunk_index + 1}"),
                    revision=str(metadata.get("revision") or item.get("updated_at") or "current"),
                    title=str(item.get("knowledge_title") or item.get("knowledge_filename") or "知识文档"),
                    summary=str(item.get("content") or item.get("excerpt") or "")[:1200],
                )
            )
        return evidence

    @staticmethod
    def _wiki_query(query: str) -> str:
        cleaned = re.sub(
            r"(请|帮我|查询|分析|一下|是什么|如何|怎么|给出|说明|来源|业务口径|口径)",
            " ",
            query,
        )
        tokens = re.findall(r"[\u4e00-\u9fffA-Za-z0-9_+-]{2,}", cleaned)
        expanded: list[str] = []
        for token in tokens:
            expanded.append(token)
            if len(token) >= 5:
                expanded.extend(token[index : index + 4] for index in range(len(token) - 3))
                # The leading concept is usually more discriminative in Chinese
                # domain-specific terms embedded in longer business questions.
                expanded.append(token[:2])
        unique = list(dict.fromkeys(item for item in expanded if item.strip()))[:8]
        if not unique:
            unique = [query.strip()]
        return "|".join(re.escape(item) for item in unique)

    async def _wiki_search(self, kb_id: str, query: str, limit: int = 10) -> list[dict[str, Any]]:
        data, _ = await self._request(
            "GET",
            f"/knowledgebase/{kb_id}/wiki/search",
            params={"q": query, "limit": limit},
        )
        if isinstance(data, dict):
            rows = data.get("pages") or data.get("results") or []
        else:
            rows = data if isinstance(data, list) else []
        return [item for item in rows if isinstance(item, dict)]

    async def _wiki_page(self, kb_id: str, slug: str) -> dict[str, Any]:
        data, _ = await self._request(
            "GET",
            f"/knowledgebase/{kb_id}/wiki/pages/{quote(slug, safe='/')}",
        )
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _page_evidence(kb_id: str, page: dict[str, Any]) -> EvidenceRef:
        source_refs = page.get("source_refs") if isinstance(page.get("source_refs"), list) else []
        chunk_refs = page.get("chunk_refs") if isinstance(page.get("chunk_refs"), list) else []
        slug = str(page.get("slug") or "unknown")
        content = str(page.get("content") or page.get("summary") or "")
        return EvidenceRef(
            knowledge_base_id=kb_id,
            document_id=str(source_refs[0] if source_refs else page.get("id") or slug),
            chunk_id=str(chunk_refs[0] if chunk_refs else page.get("id") or slug),
            locator=f"Wiki: {slug}",
            revision=f"v{page.get('version') or 'current'}",
            title=str(page.get("title") or slug),
            summary=content[:1200],
        )

    async def research(self, query: str, kb_ids: list[str] | None = None) -> KnowledgeResearchResult:
        from semora_agent.model import structured_model
        if not structured_model.configured:
            if structured_model.required:
                raise KnowledgeCoreError('请先配置大模型，才能执行知识研究')
            return await self._deterministic_research(query,kb_ids)
        selected_ids=list(dict.fromkeys(kb_ids or []))
        if not selected_ids:
            raise KnowledgeCoreError('请先选择需要研究的知识库')
        observations=[]
        steps=[]
        pages=[]
        allowed_pages=set()
        allowed_sources=set()
        visited=set()
        answer=''
        scope_errors=0
        for iteration in range(8):
            decision=await structured_model.complete_json(
                '''你是通用业务知识研究员。根据问题和已有观察决定下一步，只输出 JSON。
不预设行业或业务口径，只使用用户问题、绑定知识库和实际检索内容，不引入其他业务域的默认定义。
action 可选 wiki_search/wiki_read_page/read_source/finish；参数 kb_id、query、slug、document_id、page。
先用关键词检索入口，再按证据缺口阅读全文、沿关联slug展开，重要口径回溯原始文档。知识库是可选辅助，找不到则finish并说明未命中，不能凭记忆编造来源。
wiki_search 每次只传一个来自当前问题或观察的短关键词；不能用空格拼接多个业务主题作为一次查询。收到结果后再决定下次调用；零命中换短词/别名继续，命中后先阅读相关页。
只有一个绑定知识库时可省略 kb_id；存在多个库时必须从给定kb_ids中选择准确ID，不能使用库名称、占位符或编造ID。观察中的参数错误需要修正后再调用。
只允许使用给定kb_id和观察中实际出现的slug、document_id。finish时输出 summary，明确来源事实、冲突、未知项。不要输出隐藏思维链。最多8步，合理结束。''',
                json.dumps({'question':query,'kb_ids':selected_ids,'observations':observations,'remaining_steps':8-iteration},ensure_ascii=False),
            )
            action=decision.get('action')
            if action=='finish':
                answer=str(decision.get('summary') or '')
                break
            kb_id=str(decision.get('kb_id') or '').strip()
            if not kb_id and len(selected_ids)==1:
                kb_id=selected_ids[0]
            if kb_id not in selected_ids:
                scope_errors+=1
                if scope_errors>=3:
                    raise KnowledgeCoreError('模型连续3次提交无效知识库ID，未访问未选择的知识库；请选择准确的知识库后重试')
                observations.append({'action':action,'error':'invalid_knowledge_base','allowed_kb_ids':selected_ids,'message':'kb_id缺失或不在当前选择范围，请使用allowed_kb_ids中的准确ID重新调用。未访问其他知识库。'})
                steps.append(KnowledgeResearchStep(kind='validation',tool=action,summary='知识库参数无效，已拦截并要求模型修正，未访问其他知识库'))
                continue
            scope_errors=0
            if action=='wiki_search':
                term=str(decision.get('query') or '').strip()[:200]
                terms=[v for v in re.split(r'[\s,，、;；|]+',term) if v]
                if not term or len(term)>64 or len(terms)!=1 or re.search(r'[\s,，、;；|]',term):
                    observations.append({'action':action,'error':'single_keyword_required','suggested_keywords':terms[:6],'message':'一次只允许一个短关键词，请选一个词重新调用；本次没有发出检索请求。'})
                    steps.append(KnowledgeResearchStep(kind='validation',tool=action,summary='多词或无效检索表达式已拦截，要求模型逐词检索'))
                    continue
                rows=await self._wiki_search(kb_id,re.escape(term))
                for row in rows:
                    if row.get('slug'): allowed_pages.add((kb_id,row['slug']))
                observation={'action':action,'kb_id':kb_id,'query':term,'count':len(rows),'message':'此词未命中，请换更短词或别名继续，不能据此判断知识库没有资料' if not rows else '请阅读相关页，再根据证据缺口决定下一次检索','pages':[{'slug':r.get('slug'),'title':r.get('title'),'summary':str(r.get('summary') or r.get('content') or '')[:600]} for r in rows]}
                summary=f'检索“{term}”，找到 {len(rows)} 个页面'
            elif action=='wiki_read_page':
                slug=str(decision.get('slug') or '')
                if (kb_id,slug) not in allowed_pages: raise KnowledgeCoreError('模型请求了未经检索发现的 Wiki 页面')
                if (kb_id,slug) in visited:
                    observations.append({'action':action,'message':'该页已阅读，请继续研究其他缺口或结束'})
                    continue
                visited.add((kb_id,slug))
                page=await self._wiki_page(kb_id,slug)
                pages.append((kb_id,page))
                links=[str(v.get('slug') if isinstance(v,dict) else v) for v in (page.get('out_links') or [])+(page.get('in_links') or [])]
                for link in links: allowed_pages.add((kb_id,link))
                for source in page.get('source_refs') or []:
                    if isinstance(source,str): allowed_sources.add((kb_id,source))
                observation={'action':action,'kb_id':kb_id,'slug':slug,'title':page.get('title'),'content':str(page.get('content') or '')[:8000],'links':links[:30],'source_refs':page.get('source_refs') or []}
                summary=f'阅读 Wiki：{page.get("title") or slug}，发现 {len(links)} 个关联页'
            elif action=='read_source':
                source=str(decision.get('document_id') or '')
                if (kb_id,source) not in allowed_sources: raise KnowledgeCoreError('模型请求了未经证据引用的原始文档')
                number=max(1,min(20,int(decision.get('page') or 1)))
                chunks,_=await self._request('GET',f'/chunks/{source}',params={'page':number,'page_size':10})
                chunks=chunks if isinstance(chunks,list) else []
                observation={'action':action,'document_id':source,'page':number,'chunks':[{'id':r.get('id'),'content':str(r.get('content') or '')[:3000]} for r in chunks]}
                summary=f'回溯原始文档 {source} 第 {number} 页分块'
            else:
                raise KnowledgeCoreError('模型返回了不支持的知识研究操作')
            observations.append(observation)
            steps.append(KnowledgeResearchStep(kind='tool_result',tool=action,summary=summary))
        evidence=[self._page_evidence(kb,page) for kb,page in pages]
        steps.append(KnowledgeResearchStep(kind='completed',summary=f'研究完成，{len(evidence)} 条辅助证据；未获得证据时继续需求分析'))
        return KnowledgeResearchResult(provider=self.provider,agent_id='semora-llm-wiki-researcher',session_id='',answer=answer or '已完成受限知识研究，请结合证据继续分析需求。',steps=steps,evidence=evidence)

    async def _deterministic_research(self, query: str, kb_ids: list[str] | None = None) -> KnowledgeResearchResult:
        selected_ids = [item for item in (kb_ids or []) if item]
        if not selected_ids:
            raise KnowledgeCoreError("Wiki Researcher 至少需要绑定一个 WeKnora 知识库")

        search_query = self._wiki_query(query)
        steps = [
            KnowledgeResearchStep(
                kind="planning",
                tool="wiki_researcher",
                summary=f"根据问题生成 Wiki 检索式：{search_query}",
            )
        ]
        candidates: list[tuple[str, dict[str, Any]]] = []
        for kb_id in selected_ids:
            steps.append(
                KnowledgeResearchStep(
                    kind="tool_call",
                    tool="wiki_search",
                    summary=f"在知识库 {kb_id} 中检索 Wiki 页面",
                    status="running",
                )
            )
            rows = await self._wiki_search(kb_id, search_query)
            candidates.extend((kb_id, row) for row in rows)
            steps.append(
                KnowledgeResearchStep(
                    kind="tool_result",
                    tool="wiki_search",
                    summary=f"知识库 {kb_id} 返回 {len(rows)} 个 Wiki 入口页面",
                )
            )
        if not candidates:
            raise KnowledgeCoreError("WeKnora Wiki 未检索到相关页面，已停止 SQL 推理")

        pages: list[tuple[str, dict[str, Any]]] = []
        visited: set[tuple[str, str]] = set()
        frontier: list[tuple[str, str]] = []
        for kb_id, item in candidates[:5]:
            slug = str(item.get("slug") or "")
            if slug:
                frontier.append((kb_id, slug))

        while frontier and len(pages) < 10:
            kb_id, slug = frontier.pop(0)
            key = (kb_id, slug)
            if key in visited:
                continue
            visited.add(key)
            steps.append(
                KnowledgeResearchStep(
                    kind="tool_call",
                    tool="wiki_read_page",
                    summary=f"读取 [[{slug}]] 全文、来源与关联关系",
                    status="running",
                )
            )
            page = await self._wiki_page(kb_id, slug)
            if not page:
                continue
            pages.append((kb_id, page))
            links = [
                str(value.get("slug") if isinstance(value, dict) else value)
                for value in (page.get("out_links") or []) + (page.get("in_links") or [])
            ]
            links = [value for value in links if value and value != "None"]
            steps.append(
                KnowledgeResearchStep(
                    kind="tool_result",
                    tool="wiki_read_page",
                    summary=(
                        f"读取“{page.get('title') or slug}”，找到 {len(page.get('source_refs') or [])} 个原始来源、"
                        f"{len(page.get('chunk_refs') or [])} 个分块引用和 {len(links)} 个关联页面"
                    ),
                )
            )
            if len(pages) <= 3:
                for linked_slug in links[:4]:
                    linked_key = (kb_id, linked_slug)
                    if linked_key not in visited and linked_key not in frontier:
                        frontier.append(linked_key)

        evidence = [self._page_evidence(kb_id, page) for kb_id, page in pages]
        evidence = list({(item.knowledge_base_id, item.locator): item for item in evidence}.values())
        if not evidence:
            raise KnowledgeCoreError("WeKnora Wiki 页面缺少可溯源证据，已停止 SQL 推理")
        steps.append(
            KnowledgeResearchStep(
                kind="completed",
                tool="wiki_researcher",
                summary=f"完成入口检索、关联图展开和来源核验，形成 {len(evidence)} 条证据",
            )
        )
        answer = "\n".join(
            f"- [[{item.locator.removeprefix('Wiki: ')}|{item.title}]]：{item.summary[:240]}"
            for item in evidence[:8]
        )
        return KnowledgeResearchResult(
            provider=self.provider,
            agent_id="semora-weknora-wiki-researcher",
            session_id="",
            answer=answer,
            steps=steps,
            evidence=evidence,
        )

    async def search_evidence(self, query: str, kb_ids: list[str] | None = None) -> list[EvidenceRef]:
        selected_ids = kb_ids or []
        data, _ = await self._request(
            "POST",
            "/knowledge-search",
            json={"query": query, "knowledge_base_ids": selected_ids},
        )
        rows = data if isinstance(data, list) else []
        return self._map_evidence(rows, selected_ids)


def build_knowledge_service() -> KnowledgeCoreClient | WeKnoraClient:
    provider = os.getenv("KNOWLEDGE_PROVIDER", "weknora").strip().lower()
    if provider == "weknora":
        return WeKnoraClient()
    if provider == "native":
        return KnowledgeCoreClient()
    raise RuntimeError("KNOWLEDGE_PROVIDER must be weknora or native")


knowledge_service = build_knowledge_service()
