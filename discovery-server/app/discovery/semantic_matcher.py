import asyncio
import os
import time
import warnings
from functools import lru_cache
from typing import TYPE_CHECKING, Any

import numpy as np
from openai import AsyncOpenAI, OpenAIError
from sqlmodel import select

from app.core.config import settings
from app.core.database import get_async_session_context
from app.core.logging_config import get_logger
from app.discovery.singleton import AgentDiscovery
from app.sync.model import Skill

if TYPE_CHECKING:
    from FlagEmbedding import BGEM3FlagModel

logger = get_logger(__name__)

EMBEDDING_BATCH_INFERENCE_ERRORS = (OSError, RuntimeError, ValueError, TypeError)
EMBEDDING_WORKER_ERRORS = (*EMBEDDING_BATCH_INFERENCE_ERRORS, IndexError, KeyError)

type EmbeddingResult = dict[str, Any]
type EmbeddingQueueItem = tuple[str, asyncio.Future[EmbeddingResult], bool, bool]


@lru_cache(maxsize=1)
def _load_bgem3_flag_model() -> Any:
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r"builtin type SwigPyPacked has no __module__ attribute",
            category=DeprecationWarning,
        )
        warnings.filterwarnings(
            "ignore",
            message=r"builtin type SwigPyObject has no __module__ attribute",
            category=DeprecationWarning,
        )
        warnings.filterwarnings(
            "ignore",
            message=r"builtin type swigvarlink has no __module__ attribute",
            category=DeprecationWarning,
        )
        try:
            from FlagEmbedding import BGEM3FlagModel
        except ImportError as exc:
            # GPU 本地模型栈（torch/FlagEmbedding 等）现在只在 `gpu` extra 里声明，base
            # 环境不会安装。裸 ModuleNotFoundError 对运维/开发者不够直观，这里转换成
            # RuntimeError（落在 SEMANTIC_MATCHER_STARTUP_ERRORS 内，会被 lifespan 优雅
            # 降级为"语义匹配器未启动"而不是让进程崩溃），并给出明确的安装指引。
            raise RuntimeError(
                "GPU 模式需要本地模型推理依赖（torch/FlagEmbedding 等），但当前环境未安装。"
                "请执行 `uv sync --extra gpu`（或安装 `discovery-server[gpu]`）后重试，"
                "或将 DISCOVERY_MODE 切换为 cpu 使用远端 Embedding/LLM API。"
            ) from exc

    return BGEM3FlagModel


class SemanticAgentMatcher:
    """基于语义相似度的智能体匹配器"""

    def __init__(
        self,
        mode: str | None = None,
        model_path: str | None = None,  # 改为模型路径
        devices: list[str] | None = None,  # 添加设备参数
        reranker_url: str | None = None,  # 改为URL参数
        api_key: str | None = None,
        base_url: str | None = None,
        model_name: str | None = None,
        batch_size: int = 64,  # 最大batch大小
        max_wait_time: float = 0.01,  # 最大等待时间(秒)
        serial_mode: bool = True,
    ) -> None:
        """
        初始化语义匹配器

        Args:
            mode: 运行模式（cpu/gpu）
            model_path: BGE-M3模型路径
            devices: 使用的设备列表
            reranker_url: Reranker服务URL
            api_key: CPU模式下 Embedding LLM 的 API Key
            base_url: CPU模式下 Embedding LLM 的 Base URL
            model_name: CPU模式下 Embedding 模型名称
            batch_size: 批处理大小，建议32-64
            max_wait_time: 最大等待时间，平衡延迟和吞吐
        """
        self.mode = (mode or settings.DISCOVERY_MODE or "cpu").strip().lower()
        self.reranker_url = reranker_url

        # CPU 模式走 Embedding LLM，不加载本地 embedding/reranker。
        self.client: AsyncOpenAI | None = None
        self.model: BGEM3FlagModel | None = None
        self.model_name: str | None = None
        if self.mode == "cpu":
            resolved_api_key = api_key or settings.EMBEDDING_API_KEY
            resolved_base_url = base_url or settings.EMBEDDING_BASE_URL
            resolved_model_name = model_name or settings.EMBEDDING_MODEL_NAME

            if not resolved_api_key:
                raise ValueError("CPU 模式缺少 EMBEDDING_API_KEY")
            if not resolved_base_url:
                raise ValueError("CPU 模式缺少 EMBEDDING_BASE_URL")
            if not resolved_model_name:
                raise ValueError("CPU 模式缺少 EMBEDDING_MODEL_NAME")

            self.client = AsyncOpenAI(
                api_key=resolved_api_key,
                base_url=resolved_base_url,
                timeout=float(
                    os.environ.get("EMBEDDING_TIMEOUT") or getattr(settings, "EMBEDDING_TIMEOUT", 180) or 180
                ),
                max_retries=int(os.environ.get("EMBEDDING_MAX_RETRIES") or "3"),
            )
            self.model_name = resolved_model_name
            self.reranker_url = ""
            logger.info("SemanticAgentMatcher 初始化为 CPU 模式（Embedding LLM）")
        else:
            model_cls = _load_bgem3_flag_model()
            self.model = model_cls(
                model_path,
                query_instruction_for_retrieval="Represent this sentence for searching relevant passages:",
                query_instruction_format="{}{}",
                use_fp16=True,
                devices=devices,
            )
            self.model_name = None
            logger.info("SemanticAgentMatcher 初始化为 GPU 模式（本地 embedding 模型）")

        # 批处理队列配置
        self.batch_size = batch_size
        self.max_wait_time = max_wait_time

        # 查询和文档分别使用独立队列（encode方法不同）
        self.query_queue: asyncio.Queue[EmbeddingQueueItem] = asyncio.Queue()
        self.corpus_queue: asyncio.Queue[EmbeddingQueueItem] = asyncio.Queue()

        # 串行控制
        if serial_mode:
            self.query_model_lock = asyncio.Semaphore(1)
            self.corpus_model_lock = asyncio.Semaphore(1)
        else:
            # 并行模式：锁容量设为无限大
            self.query_model_lock = asyncio.Semaphore(999999)
            self.corpus_model_lock = asyncio.Semaphore(999999)
        # worker任务引用
        self._workers: list[asyncio.Task[None]] = []

    def start_workers(self) -> None:
        """启动批处理worker"""
        if self.mode == "cpu":
            logger.info("CPU 模式不启动本地 embedding workers")
            return
        self._workers = [
            asyncio.create_task(self._embedding_worker(self.query_queue, is_query=True)),
            asyncio.create_task(self._embedding_worker(self.corpus_queue, is_query=False)),
        ]
        logger.info("批处理workers已启动")

    @staticmethod
    def _drain_queue(queue: asyncio.Queue[EmbeddingQueueItem]) -> None:
        """取消队列中尚未完成的请求 future。"""

        while not queue.empty():
            try:
                item = queue.get_nowait()
                if len(item) >= 2 and isinstance(item[1], asyncio.Future) and not item[1].done():
                    item[1].cancel()
            except asyncio.QueueEmpty:
                break

    async def stop_workers(self) -> None:
        """停止批处理worker"""
        if self.mode == "cpu":
            return

        if not self._workers:
            return

        # 清空队列中的待处理请求
        queues = [self.query_queue, self.corpus_queue]
        for queue in queues:
            self._drain_queue(queue)

        # 取消所有worker任务
        for worker in self._workers:
            worker.cancel()

        # 等待所有worker完成
        await asyncio.gather(*self._workers, return_exceptions=True)
        logger.info("批处理workers已停止")

    async def _collect_batch(self, queue: asyncio.Queue[EmbeddingQueueItem]) -> list[EmbeddingQueueItem]:
        """在批处理窗口内收集待推理请求。"""

        batch: list[EmbeddingQueueItem] = []

        try:
            item = await asyncio.wait_for(queue.get(), timeout=1.0)
            batch.append(item)
        except TimeoutError:
            return batch

        start_time = time.time()
        while len(batch) < self.batch_size:
            remaining_time = self.max_wait_time - (time.time() - start_time)
            if remaining_time <= 0:
                break

            try:
                item = await asyncio.wait_for(queue.get(), timeout=min(remaining_time, 0.001))
                batch.append(item)
            except TimeoutError:
                continue

        return batch

    async def _run_batch_inference(
        self,
        batch: list[EmbeddingQueueItem],
        *,
        is_query: bool,
        model_lock: asyncio.Semaphore,
    ) -> tuple[dict[str, Any], bool, bool]:
        """执行一批 embedding 推理。"""

        model = self.model
        if model is None:
            raise RuntimeError("GPU 模式未初始化本地 embedding 模型")

        texts = [item[0] for item in batch]
        return_dense = batch[0][2] if len(batch[0]) > 2 else True
        return_sparse = batch[0][3] if len(batch[0]) > 3 else False

        loop = asyncio.get_running_loop()
        async with model_lock:
            if is_query:
                embeddings = await loop.run_in_executor(
                    None,
                    lambda: model.encode_queries(
                        texts,
                        return_dense=return_dense,
                        return_sparse=return_sparse,
                        return_colbert_vecs=False,
                    ),
                )
            else:
                embeddings = await loop.run_in_executor(
                    None,
                    lambda: model.encode_corpus(
                        texts,
                        return_dense=return_dense,
                        return_sparse=return_sparse,
                        return_colbert_vecs=False,
                    ),
                )

        return embeddings, return_dense, return_sparse

    @staticmethod
    def _set_batch_exception(batch: list[EmbeddingQueueItem], exc: Exception) -> None:
        """将异常回填给 batch 内所有未完成的 future。"""

        for item in batch:
            future = item[1]
            if not future.done():
                future.set_exception(exc)

    @staticmethod
    def _publish_batch_results(
        batch: list[EmbeddingQueueItem],
        embeddings: dict[str, Any],
        *,
        return_dense: bool,
        return_sparse: bool,
    ) -> None:
        """将推理结果回填给 batch 内 future。"""

        for (_, future, *_), idx in zip(batch, range(len(batch)), strict=False):
            if future.done():
                continue

            result = {}
            if return_dense:
                result["dense_vecs"] = embeddings["dense_vecs"][idx : idx + 1]
            if return_sparse:
                result["lexical_weights"] = embeddings["lexical_weights"][idx : idx + 1]
            future.set_result(result)

    async def _embedding_worker(self, queue: asyncio.Queue[EmbeddingQueueItem], is_query: bool) -> None:
        """
        批处理worker

        Args:
            queue: 请求队列
            is_query: 是否为查询模式
        """
        model_lock = self.query_model_lock if is_query else self.corpus_model_lock

        while True:
            try:
                batch = await self._collect_batch(queue)
                if not batch:
                    continue

                try:
                    embeddings, return_dense, return_sparse = await self._run_batch_inference(
                        batch,
                        is_query=is_query,
                        model_lock=model_lock,
                    )
                except EMBEDDING_BATCH_INFERENCE_ERRORS as exc:
                    logger.exception("模型推理失败", is_query=is_query, error=str(exc))
                    self._set_batch_exception(batch, exc)
                    continue

                self._publish_batch_results(
                    batch,
                    embeddings,
                    return_dense=return_dense,
                    return_sparse=return_sparse,
                )

                logger.info(
                    "批处理完成",
                    batch_size=len(batch),
                    is_query=is_query,
                    queue_remaining=queue.qsize(),
                )

            except asyncio.CancelledError:
                logger.info("Worker 被取消", is_query=is_query)
                raise
            except EMBEDDING_WORKER_ERRORS as exc:
                logger.exception("Worker 执行失败", is_query=is_query, error=str(exc))
                if "batch" in locals():
                    self._set_batch_exception(batch, exc)

    async def _call_reranker_api(self, query: str, texts: list[str]) -> list[float]:
        """
        调用reranker服务接口

        Args:
            query: 查询文本
            texts: 候选文本列表

        Returns:
            分数列表（与texts顺序对应）
        """
        import aiohttp

        try:
            reranker_url = self.reranker_url
            if not reranker_url:
                raise RuntimeError("Reranker URL 未配置")

            payload = {"query": query, "texts": texts}
            timeout = aiohttp.ClientTimeout(total=30)

            async with (
                aiohttp.ClientSession() as session,
                session.post(
                    reranker_url,
                    json=payload,
                    timeout=timeout,
                ) as response,
            ):
                if response.status != 200:
                    raise RuntimeError(f"Reranker API返回错误: {response.status}")

                result = await response.json()
                sorted_result = sorted(result, key=lambda x: x["index"])
                return [item["score"] for item in sorted_result]

        except (aiohttp.ClientError, TimeoutError, RuntimeError, TypeError, ValueError, KeyError) as e:
            logger.exception("调用 Reranker API 失败", error=str(e))
            raise

    async def _get_embedding_from_api(
        self,
        text: str,
        return_dense: bool = True,
        return_sparse: bool = False,
    ) -> EmbeddingResult:
        """CPU 模式：通过 Embedding LLM 生成单条向量。"""
        if self.client is None:
            raise RuntimeError("CPU 模式未初始化 Embedding LLM 客户端")
        model_name = self.model_name
        if model_name is None:
            raise RuntimeError("CPU 模式未配置 Embedding 模型名称")

        response = await self.client.embeddings.create(
            model=model_name,
            input=text,
            dimensions=settings.EMBEDDING_DIM,
        )
        dense_vector = np.asarray(response.data[0].embedding, dtype=np.float32)

        result: dict[str, Any] = {}
        if return_dense:
            result["dense_vecs"] = [dense_vector]
        if return_sparse:
            # Embedding LLM 不返回稀疏向量，使用空字典占位保持调用方兼容。
            result["lexical_weights"] = [{}]
        return result

    async def _get_embeddings_batch_from_api(
        self,
        texts: list[str],
        return_dense: bool = True,
        return_sparse: bool = False,
    ) -> list[EmbeddingResult]:
        """CPU 模式：批量通过 Embedding LLM 生成向量。"""
        if self.client is None:
            raise RuntimeError("CPU 模式未初始化 Embedding LLM 客户端")
        model_name = self.model_name
        if model_name is None:
            raise RuntimeError("CPU 模式未配置 Embedding 模型名称")

        if not texts:
            return []

        try:
            response = await self.client.embeddings.create(
                model=model_name,
                input=texts,
                dimensions=settings.EMBEDDING_DIM,
            )
            embeddings = [np.asarray(item.embedding, dtype=np.float32) for item in response.data]

            results: list[dict[str, Any]] = []
            for dense_vector in embeddings:
                result: dict[str, Any] = {}
                if return_dense:
                    result["dense_vecs"] = [dense_vector]
                if return_sparse:
                    result["lexical_weights"] = [{}]
                results.append(result)
            return results
        except (OpenAIError, RuntimeError, TypeError, ValueError) as exc:
            if len(texts) <= 1:
                raise
            logger.warning("批量 Embedding 调用失败，降级逐条调用", error=str(exc))
            results = []
            for text in texts:
                results.append(
                    await self._get_embedding_from_api(
                        text,
                        return_dense=return_dense,
                        return_sparse=return_sparse,
                    )
                )
            return results

    async def _get_embedding(
        self, text: str, is_query: bool = False, return_dense: bool = True, return_sparse: bool = False
    ) -> EmbeddingResult:
        """
        异步获取单个embedding（通过队列）

        Args:
            text: 文本
            is_query: 是否为查询模式
            return_dense: 是否返回稠密向量
            return_sparse: 是否返回稀疏向量

        Returns:
            embedding字典，包含dense_vecs和/或lexical_weights
        """
        if self.mode == "cpu":
            return await self._get_embedding_from_api(
                text,
                return_dense=return_dense,
                return_sparse=return_sparse,
            )

        loop = asyncio.get_event_loop()
        future: asyncio.Future[EmbeddingResult] = loop.create_future()

        # 选择对应队列
        queue = self.query_queue if is_query else self.corpus_queue
        await queue.put((text, future, return_dense, return_sparse))

        # 等待worker处理
        return await future

    async def _get_embeddings_batch(
        self, texts: list[str], is_query: bool = False, return_dense: bool = True, return_sparse: bool = False
    ) -> list[EmbeddingResult]:
        """
        批量获取embeddings（通过队列，会自动合并）

        Args:
            texts: 文本列表
            is_query: 是否为查询模式
            return_dense: 是否返回稠密向量
            return_sparse: 是否返回稀疏向量

        Returns:
            embedding字典列表
        """
        if self.mode == "cpu":
            return await self._get_embeddings_batch_from_api(
                texts,
                return_dense=return_dense,
                return_sparse=return_sparse,
            )

        # 并发提交所有请求
        tasks = [self._get_embedding(text, is_query, return_dense, return_sparse) for text in texts]
        return await asyncio.gather(*tasks)

    async def rerank_results(
        self, query: str, candidates: list[dict[str, Any]], top_k: int | None = None
    ) -> list[dict[str, Any]]:
        """
        使用reranker接口对候选结果进行重排序

        Args:
            query: 查询文本
            candidates: 候选结果列表，每个元素需包含'description'字段
            top_k: 返回前k个结果，None表示返回全部

        Returns:
            重排序后的候选结果列表
        """
        import aiohttp

        if not self.reranker_url or not candidates:
            return candidates

        try:
            # 提取所有候选文本
            texts = [candidate.get("description", "") for candidate in candidates]

            scores = await self._call_reranker_api(query, texts)

            # 将分数添加到候选结果中
            for candidate, score in zip(candidates, scores, strict=False):
                candidate["rerank_score"] = float(score)

            # 按rerank分数排序
            candidates.sort(key=lambda x: x.get("rerank_score", 0), reverse=True)

            # 返回top_k结果
            if top_k:
                candidates = candidates[:top_k]

            logger.info("Rerank 完成", result_count=len(candidates))

            return candidates

        except (aiohttp.ClientError, TimeoutError, RuntimeError, TypeError, ValueError, KeyError) as e:
            logger.exception("Rerank 失败", error=str(e))
            return candidates

    async def _update_agent_index(self, agent_data: dict[str, Any]) -> None:
        """更新单个智能体的语义索引 - 拆解为skill并存储到数据库"""
        agent_aic = agent_data.get("aic", agent_data.get("AIC", ""))
        if not agent_aic:
            return

        # 1. 拆解智能体为skill候选列表（复用现有逻辑）
        skill_candidates = AgentDiscovery._expand_agents_to_skills([agent_data])

        # 2. 收集所有需要生成embedding的描述文本
        descriptions_to_embed = []
        for skill in skill_candidates:
            description = skill.get("description", "")
            if description:
                descriptions_to_embed.append(description)

        # 3. 批量生成embeddings(同时生成稠密和稀疏向量)
        if descriptions_to_embed:
            embeddings = await self._get_embeddings_batch(
                descriptions_to_embed, is_query=False, return_dense=True, return_sparse=True
            )

            # 4. 存储到数据库
            async with get_async_session_context() as session, session.begin():
                # 先删除该agent的所有旧skill记录
                delete_stmt = select(Skill).where(Skill.aic == agent_aic)
                result = await session.execute(delete_stmt)
                old_skills = result.scalars().all()
                for old_skill in old_skills:
                    await session.delete(old_skill)

                # 插入新的skill记录
                for skill_candidate, embedding_dict in zip(skill_candidates, embeddings, strict=False):
                    # 转换稀疏向量格式: {token_id: weight}
                    sparse_embedding = None
                    if "lexical_weights" in embedding_dict:
                        # lexical_weights 格式为 [{token: weight, ...}]
                        lexical_weights = embedding_dict["lexical_weights"][0]
                        # 将token转换为字符串作为key
                        sparse_embedding = {str(token): float(weight) for token, weight in lexical_weights.items()}

                    skill_record = Skill(
                        aic=agent_aic,
                        skill_id=skill_candidate.get("skillid", ""),
                        description=skill_candidate.get("description", ""),
                        embedding=embedding_dict["dense_vecs"][0].tolist(),  # 稠密向量
                        sparse_embedding=sparse_embedding,  # 稀疏向量
                    )
                    session.add(skill_record)

        logger.info("智能体索引更新成功", agent_aic=agent_aic, skill_count=len(skill_candidates))
