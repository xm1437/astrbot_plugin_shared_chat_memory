"""
astrbot_plugin_shared_chat_memory
================================

跨会话共享聊天记忆插件。

支持两种存储/检索后端，可在 WebUI 设置界面自由切换或同时启用：

1. **本地 SQLite + BM25**：纯 Python 标准库实现，零依赖、零成本，开箱即用。
   - 用 SQLite 持久化记忆文档
   - 用 BM25 算法做关键词匹配召回（适合中文/英文/混合文本）

2. **AstrBot 知识库 API**：调用 AstrBot 原生知识库系统做向量存储与检索。
   - 需要 Embedding 嵌入模型提供商
   - 可自动创建知识库
   - 向量检索对语义理解更强（能识别同义词）

工作流程：
1. 监听用户消息与机器人回复，把每次「问-答」对作为一条记忆文档写入已启用的后端
2. 每当用户向机器人发起新提问时，插件先用所有已启用后端检索相关历史
3. 合并召回结果并按分数排序，注入到 LLM 系统提示中

作者: anonymous
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import sqlite3
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.provider import ProviderRequest, LLMResponse
from astrbot.api.star import Context, Star, register
from astrbot.api import AstrBotConfig

# 尝试导入 aiohttp（AstrBot 环境通常已安装），用于调用 Embedding API
try:
    import aiohttp
    _HAS_AIOHTTP = True
except ImportError:
    _HAS_AIOHTTP = False


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

_DOC_SOURCE_TAG = "shared_chat_memory"  # 写入知识库文档的来源标识
_DOC_NAME_PREFIX = "scm_"               # 写入知识库文档名前缀


# ---------------------------------------------------------------------------
# 文本处理工具
# ---------------------------------------------------------------------------

# 中文 CJK 字符范围
_CJK_PATTERN = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")


def _safe_text(text: Any, max_len: int = 2000) -> str:
    """把任意对象转换为安全可读字符串，并按上限截断。"""
    if text is None:
        return ""
    if isinstance(text, str):
        s = text
    elif isinstance(text, (list, tuple)):
        # 消息链：抽出 Plain 文本
        parts = []
        for comp in text:
            if isinstance(comp, str):
                parts.append(comp)
            elif hasattr(comp, "text"):
                parts.append(getattr(comp, "text") or "")
            elif hasattr(comp, "data") and isinstance(comp.data, dict):
                parts.append(comp.data.get("text", "") or "")
        s = "".join(parts)
    else:
        s = str(text)
    s = s.strip()
    if max_len and len(s) > max_len:
        s = s[:max_len] + "…"
    return s


def _tokenize(text: str) -> List[str]:
    """
    简易分词器，同时处理中英文：
    - 英文/数字：按非字母数字字符切分，并小写化
    - 中文：按字符切分（每个汉字作为一个 token）
    这样能兼顾中英文混合文本，且不依赖任何第三方库。
    """
    if not text:
        return []
    tokens: List[str] = []
    # 先把英文/数字部分切出来
    for word in re.findall(r"[A-Za-z0-9_]+", text):
        if word:
            tokens.append(word.lower())
    # 再把中文按字符切分
    for ch in text:
        if _CJK_PATTERN.match(ch):
            tokens.append(ch)
    return tokens


def _contains_any(text: str, keywords: List[str]) -> bool:
    """检查文本是否包含任一关键词（大小写不敏感）。"""
    if not keywords:
        return False
    text_lower = text.lower()
    return any(k.strip().lower() in text_lower for k in keywords if k and k.strip())


def _now_str() -> str:
    """当前时间字符串。"""
    return time.strftime("%Y%m%d_%H%M%S", time.localtime())


# ---------------------------------------------------------------------------
# 后端 1：本地 SQLite + BM25
# ---------------------------------------------------------------------------

class LocalBM25Backend:
    """
    纯本地实现，使用 SQLite 持久化记忆文档，使用 BM25 算法做关键词匹配召回。

    特点：
    - 零依赖：仅用 Python 标准库 sqlite3、re、math
    - 线程安全：每个连接独立，使用 threading.Lock 保护写入
    - 持久化：所有记忆存在本地 .db 文件中，重启不丢失
    """

    def __init__(self, db_path: str, max_records: int = 10000) -> None:
        # 解析为绝对路径（相对当前工作目录）
        if not os.path.isabs(db_path):
            db_path = os.path.abspath(db_path)
        self.db_path = db_path
        self.max_records = max_records
        self._lock = threading.Lock()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        """每个线程独立获取连接（SQLite 连接不能跨线程共享）。"""
        conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        """初始化数据库表结构。"""
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS memories (
                        id TEXT PRIMARY KEY,
                        content TEXT NOT NULL,
                        sender TEXT,
                        platform TEXT,
                        session_type TEXT,
                        umo TEXT,
                        ts REAL NOT NULL,
                        ts_str TEXT,
                        tokens TEXT
                    )
                    """
                )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_ts ON memories(ts)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_umo ON memories(umo)")
                conn.commit()
            finally:
                conn.close()
        logger.info(f"[SharedChatMemory][Local] 数据库已初始化: {self.db_path}")

    def store(self, content: str, metadata: Dict[str, Any]) -> bool:
        """写入一条记忆。"""
        if not content or not content.strip():
            return False
        with self._lock:
            conn = self._get_conn()
            try:
                mem_id = uuid.uuid4().hex
                tokens = _tokenize(content)
                ts = time.time()
                conn.execute(
                    """
                    INSERT INTO memories (id, content, sender, platform, session_type, umo, ts, ts_str, tokens)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        mem_id,
                        content,
                        metadata.get("sender", ""),
                        metadata.get("platform", ""),
                        metadata.get("session_type", ""),
                        metadata.get("umo", ""),
                        ts,
                        metadata.get("ts", _now_str()),
                        " ".join(tokens),
                    ),
                )
                conn.commit()

                # 容量管理：超过 max_records 时删除最旧的
                if self.max_records and self.max_records > 0:
                    cur = conn.execute("SELECT COUNT(*) AS c FROM memories")
                    row = cur.fetchone()
                    count = row["c"] if row else 0
                    if count > self.max_records:
                        delete_n = count - self.max_records
                        conn.execute(
                            """
                            DELETE FROM memories WHERE id IN (
                                SELECT id FROM memories ORDER BY ts ASC LIMIT ?
                            )
                            """,
                            (delete_n,),
                        )
                        conn.commit()
                return True
            except Exception as e:
                logger.error(f"[SharedChatMemory][Local] 写入失败: {e}")
                return False
            finally:
                conn.close()

    def retrieve(self, query: str, top_k: int = 3) -> List[Dict[str, Any]]:
        """BM25 检索：返回最相关的 top_k 条记忆。"""
        if not query or not query.strip():
            return []
        query_tokens = _tokenize(query)
        if not query_tokens:
            return []

        with self._lock:
            conn = self._get_conn()
            try:
                cur = conn.execute("SELECT id, content, sender, platform, session_type, umo, ts, ts_str, tokens FROM memories")
                rows = cur.fetchall()
            finally:
                conn.close()

        if not rows:
            return []

        # 把所有文档的 tokens 解析出来
        docs: List[Tuple[str, List[str], sqlite3.Row]] = []
        for row in rows:
            tok_str = row["tokens"] or ""
            tokens = tok_str.split() if tok_str else _tokenize(row["content"] or "")
            docs.append((row["content"], tokens, row))

        # 计算 BM25
        results = self._bm25(query_tokens, docs, top_k=top_k)
        return results

    def _bm25(
        self,
        query_tokens: List[str],
        docs: List[Tuple[str, List[str], sqlite3.Row]],
        top_k: int = 3,
        k1: float = 1.5,
        b: float = 0.75,
    ) -> List[Dict[str, Any]]:
        """
        BM25 算法实现。
        - k1: 词频饱和参数，1.2~2.0
        - b:  文档长度归一化参数，0~1
        """
        N = len(docs)
        if N == 0:
            return []

        # 计算每个 query token 的文档频率 df
        df: Dict[str, int] = {}
        for _, tokens, _ in docs:
            unique = set(tokens)
            for t in unique:
                df[t] = df.get(t, 0) + 1

        # 平均文档长度
        avgdl = sum(len(tokens) for _, tokens, _ in docs) / N
        if avgdl <= 0:
            avgdl = 1.0

        scored: List[Tuple[float, sqlite3.Row, str]] = []
        for content, tokens, row in docs:
            score = 0.0
            token_count: Dict[str, int] = {}
            for t in tokens:
                token_count[t] = token_count.get(t, 0) + 1

            dl = len(tokens)
            for qt in query_tokens:
                if qt not in token_count:
                    continue
                n = df.get(qt, 0)
                if n == 0:
                    continue
                # IDF
                idf = math.log((N - n + 0.5) / (n + 0.5) + 1.0)
                # TF 饱和
                tf = token_count[qt]
                tf_norm = (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * dl / avgdl))
                score += idf * tf_norm
            if score > 0:
                scored.append((score, row, content))

        scored.sort(key=lambda x: x[0], reverse=True)
        out: List[Dict[str, Any]] = []
        for score, row, content in scored[:top_k]:
            out.append(
                {
                    "content": content,
                    "score": float(score),
                    "sender": row["sender"],
                    "platform": row["platform"],
                    "session_type": row["session_type"],
                    "umo": row["umo"],
                    "ts_str": row["ts_str"],
                    "backend": "local_bm25",
                }
            )
        return out

    def count(self) -> int:
        """返回记忆条数。"""
        with self._lock:
            conn = self._get_conn()
            try:
                cur = conn.execute("SELECT COUNT(*) AS c FROM memories")
                row = cur.fetchone()
                return int(row["c"]) if row else 0
            finally:
                conn.close()

    def clear(self) -> int:
        """清空所有记忆，返回删除的条数。"""
        with self._lock:
            conn = self._get_conn()
            try:
                cur = conn.execute("DELETE FROM memories")
                n = cur.rowcount
                conn.commit()
                return n
            finally:
                conn.close()

    def close(self) -> None:
        """关闭资源（SQLite 每次都关连接，这里保留接口）。"""
        pass


# ---------------------------------------------------------------------------
# 后端 2：本地 SQLite + Embedding 向量检索（v1.2.0 新增）
# ---------------------------------------------------------------------------

class LocalVectorBackend:
    """
    本地 SQLite + Embedding 向量检索后端。

    特点：
    - 存储：SQLite（记忆文本 + Embedding 向量，向量以 JSON 字符串存储）
    - 检索：余弦相似度（cosine similarity）
    - Embedding API：直接在插件配置中填写 API 地址与密钥，无需在 AstrBot 服务提供商中配置
    - 兼容 OpenAI 格式的 Embedding API（硅基流动、OpenAI、各种兼容服务）
    """

    def __init__(self, db_path: str, api_url: str, api_key: str, model: str,
                 timeout: int = 30, max_records: int = 10000) -> None:
        if not os.path.isabs(db_path):
            db_path = os.path.abspath(db_path)
        self.db_path = db_path
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_records = max_records
        self._lock = threading.Lock()
        self._session: Optional[Any] = None
        self._init_db()
        if not _HAS_AIOHTTP:
            logger.warning(
                "[SharedChatMemory][Vector] 未安装 aiohttp，将使用 urllib 同步调用 Embedding API，"
                "性能可能略差。建议安装 aiohttp：pip install aiohttp"
            )
        if not self.api_key:
            logger.warning(
                "[SharedChatMemory][Vector] 未配置 Embedding API Key，向量检索后端将无法工作。"
                "请在插件设置中填写 Embedding API Key。"
            )

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS vector_memories (
                        id TEXT PRIMARY KEY,
                        content TEXT NOT NULL,
                        embedding TEXT,
                        sender TEXT,
                        platform TEXT,
                        session_type TEXT,
                        umo TEXT,
                        ts REAL NOT NULL,
                        ts_str TEXT
                    )
                    """
                )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_vec_ts ON vector_memories(ts)")
                conn.commit()
            finally:
                conn.close()
        logger.info(f"[SharedChatMemory][Vector] 向量数据库已初始化: {self.db_path}")

    async def _get_embedding(self, text: str) -> Optional[List[float]]:
        """调用 Embedding API 获取文本向量。兼容 OpenAI 格式。"""
        if not self.api_key:
            return None
        url = f"{self.api_url}/embeddings"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "input": text[:8000],  # 截断防止超长
        }

        if _HAS_AIOHTTP:
            try:
                if self._session is None:
                    self._session = aiohttp.ClientSession()
                timeout = aiohttp.ClientTimeout(total=self.timeout)
                async with self._session.post(url, json=payload, headers=headers, timeout=timeout) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.error(
                            f"[SharedChatMemory][Vector] Embedding API 返回错误 {resp.status}: {body[:200]}"
                        )
                        return None
                    data = await resp.json()
                    return self._extract_embedding(data)
            except Exception as e:
                logger.error(f"[SharedChatMemory][Vector] aiohttp 调用 Embedding API 失败: {e}")
                return None
        else:
            # 回退到 urllib（同步，在线程池中执行）
            return await asyncio.to_thread(self._get_embedding_sync, url, headers, payload)

    def _get_embedding_sync(self, url: str, headers: dict, payload: dict) -> Optional[List[float]]:
        """用 urllib 同步调用 Embedding API（回退方案）。"""
        import urllib.request
        import urllib.error
        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8")
                return self._extract_embedding(json.loads(body))
        except urllib.error.HTTPError as e:
            logger.error(f"[SharedChatMemory][Vector] Embedding API HTTP 错误 {e.code}: {e.read()[:200]}")
            return None
        except Exception as e:
            logger.error(f"[SharedChatMemory][Vector] urllib 调用 Embedding API 失败: {e}")
            return None

    def _extract_embedding(self, data: dict) -> Optional[List[float]]:
        """从 API 响应中提取 embedding 向量。"""
        if not isinstance(data, dict):
            return None
        # OpenAI 格式: {"data": [{"embedding": [...]}]}
        data_list = data.get("data")
        if isinstance(data_list, list) and data_list:
            emb = data_list[0].get("embedding") if isinstance(data_list[0], dict) else None
            if isinstance(emb, list):
                return emb
        # 某些 API 可能返回 {"embedding": [...]}
        emb = data.get("embedding")
        if isinstance(emb, list):
            return emb
        logger.error(f"[SharedChatMemory][Vector] 无法从 API 响应中提取 embedding: {str(data)[:200]}")
        return None

    @staticmethod
    def _cosine_similarity(a: List[float], b: List[float]) -> float:
        """计算两个向量的余弦相似度。"""
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = 0.0
        norm_a = 0.0
        norm_b = 0.0
        for x, y in zip(a, b):
            dot += x * y
            norm_a += x * x
            norm_b += y * y
        if norm_a <= 0 or norm_b <= 0:
            return 0.0
        return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))

    async def store(self, content: str, metadata: Dict[str, Any]) -> bool:
        """写入一条记忆（含 embedding 向量）。"""
        if not content or not content.strip():
            return False

        # 获取 embedding
        embedding = await self._get_embedding(content)
        embedding_str = json.dumps(embedding) if embedding else ""

        with self._lock:
            conn = self._get_conn()
            try:
                mem_id = uuid.uuid4().hex
                ts = time.time()
                conn.execute(
                    """
                    INSERT INTO vector_memories (id, content, embedding, sender, platform, session_type, umo, ts, ts_str)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        mem_id,
                        content,
                        embedding_str,
                        metadata.get("sender", ""),
                        metadata.get("platform", ""),
                        metadata.get("session_type", ""),
                        metadata.get("umo", ""),
                        ts,
                        metadata.get("ts", _now_str()),
                    ),
                )
                conn.commit()

                # 容量管理
                if self.max_records and self.max_records > 0:
                    cur = conn.execute("SELECT COUNT(*) AS c FROM vector_memories")
                    row = cur.fetchone()
                    count = row["c"] if row else 0
                    if count > self.max_records:
                        delete_n = count - self.max_records
                        conn.execute(
                            "DELETE FROM vector_memories WHERE id IN (SELECT id FROM vector_memories ORDER BY ts ASC LIMIT ?)",
                            (delete_n,),
                        )
                        conn.commit()
                return True
            except Exception as e:
                logger.error(f"[SharedChatMemory][Vector] 写入失败: {e}")
                return False
            finally:
                conn.close()

    async def retrieve(self, query: str, top_k: int = 3) -> List[Dict[str, Any]]:
        """向量检索：用余弦相似度返回最相关的 top_k 条记忆。"""
        if not query or not query.strip():
            return []

        # 获取查询向量
        query_emb = await self._get_embedding(query)
        if not query_emb:
            logger.debug("[SharedChatMemory][Vector] 无法获取查询向量，跳过向量检索。")
            return []

        with self._lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    "SELECT id, content, embedding, sender, platform, session_type, umo, ts_str FROM vector_memories WHERE embedding != ''"
                )
                rows = cur.fetchall()
            finally:
                conn.close()

        if not rows:
            return []

        scored: List[Tuple[float, Dict[str, Any]]] = []
        for row in rows:
            emb_str = row["embedding"] or ""
            if not emb_str:
                continue
            try:
                doc_emb = json.loads(emb_str)
            except (json.JSONDecodeError, TypeError):
                continue
            score = self._cosine_similarity(query_emb, doc_emb)
            if score > 0:
                scored.append((score, row))

        scored.sort(key=lambda x: x[0], reverse=True)
        out: List[Dict[str, Any]] = []
        for score, row in scored[:top_k]:
            out.append(
                {
                    "content": row["content"],
                    "score": float(score),
                    "sender": row["sender"],
                    "platform": row["platform"],
                    "session_type": row["session_type"],
                    "umo": row["umo"],
                    "ts_str": row["ts_str"],
                    "backend": "local_vector",
                }
            )
        return out

    def count(self) -> int:
        with self._lock:
            conn = self._get_conn()
            try:
                cur = conn.execute("SELECT COUNT(*) AS c FROM vector_memories")
                row = cur.fetchone()
                return int(row["c"]) if row else 0
            finally:
                conn.close()

    def clear(self) -> int:
        with self._lock:
            conn = self._get_conn()
            try:
                cur = conn.execute("DELETE FROM vector_memories")
                n = cur.rowcount
                conn.commit()
                return n
            finally:
                conn.close()

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None


# ---------------------------------------------------------------------------
# 后端 3：AstrBot 知识库 API
# ---------------------------------------------------------------------------

class AstrBotKBBackend:
    """
    调用 AstrBot 原生知识库系统做向量存储与检索。

    特点：
    - 需要配置 Embedding 嵌入模型提供商
    - 可自动创建知识库
    - 向量检索语义理解更强
    - 对不同 AstrBot 版本的 KB API 做了多候选方法名兼容
    """

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        self.context = context
        self.config = config
        self._target_kb_ids: Optional[List[str]] = None
        self._auto_create_attempted: bool = False

    # ----- 知识库管理器访问 -----

    def _get_kb_manager(self):
        mgr = getattr(self.context, "knowledge_base", None)
        if mgr is None:
            mgr = getattr(self.context, "knowledge_base_manager", None)
        return mgr

    def _list_kb_instances(self) -> Dict[str, Any]:
        mgr = self._get_kb_manager()
        if mgr is None:
            return {}
        kb_insts = getattr(mgr, "kb_insts", None)
        if isinstance(kb_insts, dict):
            return kb_insts
        if callable(kb_insts):
            try:
                return kb_insts() or {}
            except Exception:
                return {}
        return {}

    async def _all_kb_summaries(self) -> List[Dict[str, Any]]:
        mgr = self._get_kb_manager()
        if mgr is None:
            return []
        summaries: List[Dict[str, Any]] = []
        for method_name in ("get_all_kbs", "all_kbs", "list_kbs", "get_kbs"):
            method = getattr(mgr, method_name, None)
            if callable(method):
                try:
                    res = method()
                    if asyncio.iscoroutine(res):
                        res = await res
                    if isinstance(res, list):
                        for item in res:
                            if isinstance(item, dict):
                                summaries.append(item)
                            elif hasattr(item, "model_dump"):
                                summaries.append(item.model_dump())
                            else:
                                summaries.append({"raw": item})
                        if summaries:
                            return summaries
                except Exception as e:
                    logger.debug(f"[SharedChatMemory][KB] 调用 {method_name} 失败: {e}")
        kb_insts = self._list_kb_instances()
        for kb_id, helper in kb_insts.items():
            entry: Dict[str, Any] = {"id": kb_id, "name": kb_id}
            for attr in ("name", "kb_name", "display_name", "title"):
                v = getattr(helper, attr, None)
                if isinstance(v, str) and v:
                    entry["name"] = v
                    break
            summaries.append(entry)
        return summaries

    async def resolve_target_kb_ids(self) -> List[str]:
        """解析目标知识库 ID 列表。"""
        if self._target_kb_ids is not None:
            return self._target_kb_ids

        # 1) 配置中选择的知识库
        selected = self.config.get("kb_selector", []) or []
        if isinstance(selected, str):
            selected = [selected]
        if selected:
            self._target_kb_ids = [str(x) for x in selected]
            return self._target_kb_ids

        # 2) 自动选用系统已存在的知识库
        summaries = await self._all_kb_summaries()
        if summaries:
            preferred = next(
                (
                    s
                    for s in summaries
                    if any(
                        kw in str(s.get("name", "")).lower()
                        for kw in ("shared", "memory", "chat_memory")
                    )
                ),
                None,
            )
            pick = preferred or summaries[0]
            kb_id = str(pick.get("id") or pick.get("name") or "")
            if kb_id:
                self._target_kb_ids = [kb_id]
                return self._target_kb_ids

        # 3) 自动创建
        if self.config.get("auto_create_kb", True) and not self._auto_create_attempted:
            self._auto_create_attempted = True
            created = await self._try_create_default_kb()
            if created:
                self._target_kb_ids = [created]
                return self._target_kb_ids

        self._target_kb_ids = []
        return self._target_kb_ids

    async def _try_create_default_kb(self) -> Optional[str]:
        mgr = self._get_kb_manager()
        if mgr is None:
            logger.warning("[SharedChatMemory][KB] 无法访问知识库管理器，跳过自动创建。")
            return None

        name = self.config.get("default_kb_name", "SharedChatMemory") or "SharedChatMemory"
        desc = (
            self.config.get("default_kb_description")
            or "由 astrbot_plugin_shared_chat_memory 插件自动创建的共享聊天记忆库"
        )

        embedding_provider_id = await self._pick_embedding_provider()
        if not embedding_provider_id:
            logger.warning(
                "[SharedChatMemory][KB] 未找到可用的 Embedding 嵌入模型提供商，"
                "无法自动创建知识库。请先在 WebUI 的「服务提供商」中新增一个 Embedding 提供商。"
            )
            return None

        candidates = [
            ("create_kb", {"name": name, "description": desc, "embedding_provider_id": embedding_provider_id}),
            ("create_knowledge_base", {"name": name, "description": desc, "embedding_provider_id": embedding_provider_id}),
            ("create", {"name": name, "description": desc, "embedding_provider_id": embedding_provider_id}),
            ("new_kb", {"name": name, "description": desc, "embedding_provider_id": embedding_provider_id}),
        ]
        for method_name, kwargs in candidates:
            method = getattr(mgr, method_name, None)
            if not callable(method):
                continue
            try:
                logger.info(
                    f"[SharedChatMemory][KB] 尝试调用 {method_name} 创建知识库 '{name}'..."
                )
                result = method(**kwargs)
                if asyncio.iscoroutine(result):
                    result = await result
                kb_id = self._extract_kb_id(result)
                if kb_id:
                    if hasattr(mgr, "load_kbs"):
                        try:
                            load_res = mgr.load_kbs()
                            if asyncio.iscoroutine(load_res):
                                await load_res
                        except Exception as e:
                            logger.debug(f"[SharedChatMemory][KB] load_kbs 失败: {e}")
                    logger.info(f"[SharedChatMemory][KB] 已自动创建知识库: name={name}, id={kb_id}")
                    return kb_id
            except Exception as e:
                logger.debug(f"[SharedChatMemory][KB] 调用 {method_name} 创建知识库失败: {e}")

        logger.warning(
            "[SharedChatMemory][KB] 自动创建知识库失败，请手动在 WebUI 创建后选择。"
        )
        return None

    def _extract_kb_id(self, result: Any) -> Optional[str]:
        if result is None:
            return None
        if isinstance(result, str):
            return result
        if isinstance(result, dict):
            for key in ("id", "kb_id", "uuid", "uid"):
                if result.get(key):
                    return str(result[key])
        for attr in ("id", "kb_id", "uuid", "uid"):
            v = getattr(result, attr, None)
            if v:
                return str(v)
        if hasattr(result, "model_dump"):
            try:
                d = result.model_dump()
                for key in ("id", "kb_id", "uuid", "uid"):
                    if d.get(key):
                        return str(d[key])
            except Exception:
                pass
        return None

    async def _pick_embedding_provider(self) -> Optional[str]:
        provider_manager = getattr(self.context, "provider_manager", None)
        if provider_manager is None:
            return None

        candidates: List[Any] = []
        for attr in ("embedding_provider", "embedding_providers", "embeddings"):
            v = getattr(provider_manager, attr, None)
            if v is None:
                continue
            if isinstance(v, dict):
                candidates.extend(v.values())
            elif isinstance(v, list):
                candidates.extend(v)

        for method_name in ("get_provider_by_type", "get_providers_by_type"):
            method = getattr(provider_manager, method_name, None)
            if not callable(method):
                continue
            try:
                try:
                    from astrbot.core.provider.entities import ProviderType
                    pt = getattr(ProviderType, "embedding", None) or getattr(ProviderType, "EMBEDDING", None)
                except Exception:
                    pt = "embedding"
                res = method(pt)
                if asyncio.iscoroutine(res):
                    res = await res
                if isinstance(res, dict):
                    candidates.extend(res.values())
                elif isinstance(res, list):
                    candidates.extend(res)
            except Exception as e:
                logger.debug(f"[SharedChatMemory][KB] 调用 {method_name} 失败: {e}")

        if not candidates:
            return None

        preferred_keywords = ("bge", "embedding", "text-embedding", "m3", "embed")
        for prov in candidates:
            pid = self._get_provider_id(prov)
            if not pid:
                continue
            name_or_model = (self._get_provider_name(prov) or "") + " " + pid
            if any(k in name_or_model.lower() for k in preferred_keywords):
                return pid
        for prov in candidates:
            pid = self._get_provider_id(prov)
            if pid:
                return pid
        return None

    def _get_provider_id(self, prov: Any) -> Optional[str]:
        for attr in ("id", "provider_id", "uuid"):
            v = getattr(prov, attr, None)
            if isinstance(v, str) and v:
                return v
        if isinstance(prov, str):
            return prov
        return None

    def _get_provider_name(self, prov: Any) -> Optional[str]:
        for attr in ("name", "provider_name", "display_name", "model_name"):
            v = getattr(prov, attr, None)
            if isinstance(v, str) and v:
                return v
        return None

    # ----- 写入 / 检索 -----

    async def store(self, content: str, doc_name: str, metadata: Optional[Dict[str, Any]] = None) -> bool:
        if not content or not content.strip():
            return False
        kb_ids = await self.resolve_target_kb_ids()
        if not kb_ids:
            return False

        mgr = self._get_kb_manager()
        if mgr is None:
            return False
        kb_insts = self._list_kb_instances()
        if not kb_insts:
            if hasattr(mgr, "load_kbs"):
                try:
                    res = mgr.load_kbs()
                    if asyncio.iscoroutine(res):
                        await res
                except Exception as e:
                    logger.debug(f"[SharedChatMemory][KB] load_kbs 失败: {e}")
            kb_insts = self._list_kb_instances()

        meta_str = ""
        if metadata:
            meta_str = "\n".join(f"{k}: {v}" for k, v in metadata.items() if v is not None)
        full_text = (f"[{meta_str}]\n{content}") if meta_str else content

        for kb_id in kb_ids:
            helper = kb_insts.get(kb_id)
            if helper is None:
                continue
            ok = await self._insert_into_helper(helper, full_text, doc_name)
            if ok:
                return True
        return False

    async def _insert_into_helper(self, helper: Any, content: str, doc_name: str) -> bool:
        candidates = [
            ("insert_from_string", {"content": content, "doc_name": doc_name}),
            ("insert_from_text", {"text": content, "doc_name": doc_name}),
            ("add_text", {"text": content, "name": doc_name}),
            ("add_document", {"content": content, "doc_name": doc_name}),
            ("add_doc", {"content": content, "name": doc_name}),
            ("insert", {"content": content, "doc_name": doc_name}),
        ]
        for method_name, kwargs in candidates:
            method = getattr(helper, method_name, None)
            if not callable(method):
                continue
            try:
                res = method(**kwargs)
                if asyncio.iscoroutine(res):
                    res = await res
                return True
            except TypeError:
                continue
            except Exception as e:
                logger.debug(f"[SharedChatMemory][KB] {method_name} 异常: {e}")
                try:
                    res = method(content, doc_name)
                    if asyncio.iscoroutine(res):
                        res = await res
                    return True
                except Exception:
                    continue
        return False

    async def retrieve(self, query: str, top_k: int = 3) -> List[Dict[str, Any]]:
        if not query or not query.strip():
            return []
        kb_ids = await self.resolve_target_kb_ids()
        if not kb_ids:
            return []

        mgr = self._get_kb_manager()
        if mgr is None:
            return []

        # 优先在 manager 上检索
        for method_name in ("retrieve", "search", "query"):
            method = getattr(mgr, method_name, None)
            if not callable(method):
                continue
            try:
                for kwargs in (
                    {"query": query, "kb_ids": kb_ids, "top_k": top_k},
                    {"query": query, "kb_ids": kb_ids},
                    {"query": query, "top_k": top_k},
                    {"query": query},
                ):
                    try:
                        res = method(**kwargs)
                        if asyncio.iscoroutine(res):
                            res = await res
                        results = self._normalize_results(res)
                        if results is not None:
                            return results
                    except TypeError:
                        continue
            except Exception as e:
                logger.debug(f"[SharedChatMemory][KB] retrieve via {method_name} 失败: {e}")

        # 退而求其次：从每个 helper 检索
        kb_insts = self._list_kb_instances()
        merged: List[Dict[str, Any]] = []
        for kb_id in kb_ids:
            helper = kb_insts.get(kb_id)
            if helper is None:
                continue
            for method_name in ("retrieve", "search", "query"):
                method = getattr(helper, method_name, None)
                if not callable(method):
                    continue
                try:
                    for kwargs in ({"query": query, "top_k": top_k}, {"query": query}):
                        try:
                            res = method(**kwargs)
                            if asyncio.iscoroutine(res):
                                res = await res
                            results = self._normalize_results(res)
                            if results:
                                merged.extend(results)
                                break
                        except TypeError:
                            continue
                    if merged:
                        break
                except Exception as e:
                    logger.debug(f"[SharedChatMemory][KB] helper.{method_name} 失败: {e}")
        return merged[:top_k] if top_k > 0 else merged

    def _normalize_results(self, result: Any) -> Optional[List[Dict[str, Any]]]:
        if result is None:
            return []
        if isinstance(result, list):
            out = []
            for item in result:
                d = self._to_dict(item)
                if d is None:
                    continue
                content = d.get("content") or d.get("text") or d.get("document") or d.get("doc") or ""
                if not content and isinstance(item, str):
                    content = item
                score = d.get("score") or d.get("similarity") or d.get("relevance")
                out.append(
                    {
                        "content": content,
                        "score": score,
                        "backend": "astrbot_kb",
                        "raw": d,
                    }
                )
            return out
        if isinstance(result, dict):
            return self._normalize_results([result])
        return None

    def _to_dict(self, obj: Any) -> Optional[Dict[str, Any]]:
        if obj is None:
            return None
        if isinstance(obj, dict):
            return obj
        if hasattr(obj, "model_dump"):
            try:
                return obj.model_dump()
            except Exception:
                pass
        if hasattr(obj, "__dict__"):
            return dict(obj.__dict__)
        if isinstance(obj, str):
            return {"content": obj}
        return None

    async def count(self) -> int:
        """AstrBot KB 不易精确计数，返回 -1 表示未知。"""
        return -1

    async def clear(self) -> int:
        """按 source 标签删除本插件写入的文档。"""
        kb_ids = await self.resolve_target_kb_ids()
        if not kb_ids:
            return 0
        mgr = self._get_kb_manager()
        kb_insts = self._list_kb_instances() if mgr is not None else {}
        cleared = 0
        for kb_id in kb_ids:
            helper = kb_insts.get(kb_id)
            if helper is None:
                continue
            for method_name, kwargs in [
                ("delete_by_source", {"source": _DOC_SOURCE_TAG}),
                ("delete_by_metadata", {"key": "source", "value": _DOC_SOURCE_TAG}),
                ("delete_docs_where", {"where": f"source = '{_DOC_SOURCE_TAG}'"}),
            ]:
                method = getattr(helper, method_name, None)
                if not callable(method):
                    continue
                try:
                    res = method(**kwargs)
                    if asyncio.iscoroutine(res):
                        res = await res
                    cleared += 1
                    break
                except Exception as e:
                    logger.debug(f"[SharedChatMemory][KB] 清理调用 {method_name} 失败: {e}")
        return cleared

    def reset_cache(self) -> None:
        """重置缓存的 KB ID（用于配置变更后重新解析）。"""
        self._target_kb_ids = None
        self._auto_create_attempted = False


# ---------------------------------------------------------------------------
# 插件主类
# ---------------------------------------------------------------------------

@register(
    "astrbot_plugin_shared_chat_memory",
    "anonymous",
    "跨会话共享聊天记忆插件：将所有用户与机器人的对话写入存储后端（本地 SQLite+BM25、"
    "本地 SQLite+Embedding 向量检索、AstrBot 知识库 API），并在新提问时召回相关历史作为上下文，"
    "让机器人记住跨用户的对话。",
    "1.2.0",
    "https://github.com/AstrBotDevs/AstrBot",
)
class SharedChatMemoryPlugin(Star):
    """跨会话共享聊天记忆插件。"""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config: AstrBotConfig = config or {}

        # 后端实例
        self._local_backend: Optional[LocalBM25Backend] = None
        self._vector_backend: Optional[LocalVectorBackend] = None
        self._kb_backend: Optional[AstrBotKBBackend] = None

        # 运行时状态
        # pending_pairs[session_id] = {"user": "...", "ts": float, ...}
        self._pending_pairs: Dict[str, Dict[str, Any]] = {}
        # 写入锁
        self._write_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def _init_backends(self) -> None:
        """根据配置初始化后端实例。"""
        backend_cfg = self.config.get("backend", {}) or {}

        # 本地 BM25 后端
        if backend_cfg.get("use_local", True):
            if self._local_backend is None:
                db_path = backend_cfg.get("local_db_path", "data/shared_chat_memory.db")
                max_records = int(self.config.get("record_settings", {}).get("max_records", 10000) or 10000)
                try:
                    self._local_backend = LocalBM25Backend(db_path, max_records=max_records)
                except Exception as e:
                    logger.error(f"[SharedChatMemory] 本地 BM25 后端初始化失败: {e}")
                    self._local_backend = None
        else:
            if self._local_backend is not None:
                self._local_backend.close()
                self._local_backend = None

        # 本地向量后端
        if backend_cfg.get("use_local_vector", False):
            if self._vector_backend is None:
                db_path = backend_cfg.get("local_db_path", "data/shared_chat_memory.db")
                # 向量后端使用单独的数据库文件，避免与 BM25 后端冲突
                db_path = db_path.replace(".db", "_vector.db") if db_path.endswith(".db") else db_path + "_vector"
                max_records = int(self.config.get("record_settings", {}).get("max_records", 10000) or 10000)
                emb_cfg = self.config.get("embedding_api", {}) or {}
                try:
                    self._vector_backend = LocalVectorBackend(
                        db_path=db_path,
                        api_url=emb_cfg.get("api_url", "https://api.siliconflow.cn/v1") or "",
                        api_key=emb_cfg.get("api_key", "") or "",
                        model=emb_cfg.get("model", "BAAI/bge-m3") or "BAAI/bge-m3",
                        timeout=int(emb_cfg.get("timeout", 30) or 30),
                        max_records=max_records,
                    )
                except Exception as e:
                    logger.error(f"[SharedChatMemory] 本地向量后端初始化失败: {e}")
                    self._vector_backend = None
        else:
            if self._vector_backend is not None:
                # 异步关闭，这里用 try 兜底
                try:
                    asyncio.get_event_loop().create_task(self._vector_backend.close())
                except Exception:
                    pass
                self._vector_backend = None

        # AstrBot KB 后端
        if backend_cfg.get("use_astrbot_kb", False):
            if self._kb_backend is None:
                self._kb_backend = AstrBotKBBackend(self.context, self.config)
        else:
            self._kb_backend = None

    async def initialize(self) -> None:
        """插件初始化。"""
        if not self.config.get("enable", True):
            logger.info("[SharedChatMemory] 插件已被配置为禁用状态。")
            return

        self._init_backends()

        # 检查后端启用情况
        backends_enabled = []
        if self._local_backend is not None:
            backends_enabled.append("本地 SQLite+BM25")
        if self._vector_backend is not None:
            backends_enabled.append("本地 SQLite+Embedding 向量检索")
        if self._kb_backend is not None:
            backends_enabled.append("AstrBot 知识库 API")

        if not backends_enabled:
            logger.warning(
                "[SharedChatMemory] 未启用任何存储后端！请在插件设置中至少启用一个："
                "「本地 SQLite+BM25」（默认）或「本地 SQLite+Embedding 向量检索」"
                "或「AstrBot 知识库 API」。"
            )
            return

        logger.info(f"[SharedChatMemory] 已启用后端: {', '.join(backends_enabled)}")

        # 预解析 KB 后端的目标知识库（可能触发自动创建）
        if self._kb_backend is not None:
            try:
                kb_ids = await self._kb_backend.resolve_target_kb_ids()
                if kb_ids:
                    logger.info(f"[SharedChatMemory][KB] 已选定知识库: {kb_ids}")
                else:
                    logger.warning(
                        "[SharedChatMemory][KB] 未找到可用知识库。"
                        "请检查插件设置中的知识库选择或自动创建选项。"
                    )
            except Exception as e:
                logger.warning(f"[SharedChatMemory][KB] 解析知识库失败: {e}")

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        """AstrBot 全部加载完成后兜底重新解析 KB 后端。"""
        if not self.config.get("enable", True):
            return
        if self._kb_backend is not None:
            # 重置缓存以确保所有 provider 都初始化完成后再解析
            self._kb_backend.reset_cache()
            try:
                kb_ids = await self._kb_backend.resolve_target_kb_ids()
                if kb_ids:
                    logger.info(f"[SharedChatMemory][KB] [Loaded] 已选定知识库: {kb_ids}")
            except Exception as e:
                logger.debug(f"[SharedChatMemory][KB] [Loaded] 解析失败: {e}")

    async def terminate(self) -> None:
        """插件被卸载/停用时调用。"""
        if self._local_backend is not None:
            self._local_backend.close()
            self._local_backend = None
        if self._vector_backend is not None:
            await self._vector_backend.close()
            self._vector_backend = None
        self._pending_pairs.clear()
        logger.info("[SharedChatMemory] 插件已停止。")

    # ------------------------------------------------------------------
    # 写入 / 检索 统一调度
    # ------------------------------------------------------------------

    async def _store_memory(self, content: str, doc_name: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """把记忆写入所有已启用的后端。"""
        if not content or not content.strip():
            return
        meta = metadata or {}

        # 本地 BM25 后端
        if self._local_backend is not None:
            try:
                # 本地后端是同步方法，但放在线程池里执行避免阻塞事件循环
                await asyncio.to_thread(self._local_backend.store, content, meta)
                if self.config.get("debug", False):
                    logger.debug(f"[SharedChatMemory][Local] 写入: {doc_name}")
            except Exception as e:
                logger.error(f"[SharedChatMemory][Local] 写入失败: {e}")

        # 本地向量后端
        if self._vector_backend is not None:
            try:
                await self._vector_backend.store(content, meta)
                if self.config.get("debug", False):
                    logger.debug(f"[SharedChatMemory][Vector] 写入: {doc_name}")
            except Exception as e:
                logger.error(f"[SharedChatMemory][Vector] 写入失败: {e}")

        # KB 后端
        if self._kb_backend is not None:
            try:
                ok = await self._kb_backend.store(content, doc_name, meta)
                if not ok and self.config.get("debug", False):
                    logger.debug(f"[SharedChatMemory][KB] 写入失败: {doc_name}")
            except Exception as e:
                logger.error(f"[SharedChatMemory][KB] 写入失败: {e}")

    async def _retrieve_memory(self, query: str, top_k: int = 3) -> List[Dict[str, Any]]:
        """从所有已启用的后端检索并合并结果。"""
        if not query or not query.strip():
            return []

        results: List[Dict[str, Any]] = []

        # 本地 BM25 后端
        if self._local_backend is not None:
            try:
                local_results = await asyncio.to_thread(self._local_backend.retrieve, query, top_k)
                results.extend(local_results)
            except Exception as e:
                logger.error(f"[SharedChatMemory][Local] 检索失败: {e}")

        # 本地向量后端
        if self._vector_backend is not None:
            try:
                vec_results = await self._vector_backend.retrieve(query, top_k=top_k)
                results.extend(vec_results)
            except Exception as e:
                logger.error(f"[SharedChatMemory][Vector] 检索失败: {e}")

        # KB 后端（向量检索）
        if self._kb_backend is not None:
            try:
                kb_results = await self._kb_backend.retrieve(query, top_k=top_k)
                results.extend(kb_results)
            except Exception as e:
                logger.error(f"[SharedChatMemory][KB] 检索失败: {e}")

        # 合并后按分数排序（注意不同后端的分数量纲不同，仅做相对排序）
        # 给每个结果打个归一化分数：本地 BM25 通常 0~10，向量检索通常 0~1
        for r in results:
            score = r.get("score")
            if score is None:
                r["_norm_score"] = 0.5
            else:
                try:
                    s = float(score)
                    # 简单归一化：BM25 分数除以 5 截断到 0~1，向量分数已经是 0~1
                    if r.get("backend") == "local_bm25":
                        r["_norm_score"] = min(s / 5.0, 1.0)
                    else:
                        r["_norm_score"] = max(0.0, min(s, 1.0))
                except (TypeError, ValueError):
                    r["_norm_score"] = 0.5

        results.sort(key=lambda x: x.get("_norm_score", 0.0), reverse=True)
        return results[:top_k] if top_k > 0 else results

    # ------------------------------------------------------------------
    # 事件钩子
    # ------------------------------------------------------------------

    def _should_record_session(self, event: AstrMessageEvent) -> bool:
        if not self.config.get("enable", True):
            return False
        message_obj = event.message_obj
        is_group = bool(getattr(message_obj, "group_id", "") if message_obj else False)
        record_settings = self.config.get("record_settings", {}) or {}
        if is_group and not record_settings.get("record_group_chat", True):
            return False
        if not is_group and not record_settings.get("record_private_chat", True):
            return False
        umo = event.unified_msg_origin or ""
        whitelist = self.config.get("privacy", {}).get("session_whitelist", []) or []
        if whitelist and umo and umo not in whitelist:
            return False
        return True

    def _should_capture_message(self, text: str) -> bool:
        if not text:
            return False
        record_settings = self.config.get("record_settings", {}) or {}
        min_len = int(record_settings.get("min_message_length", 2) or 0)
        if len(text) < min_len:
            return False
        blacklist = self.config.get("privacy", {}).get("keyword_blacklist", []) or []
        if _contains_any(text, blacklist):
            return False
        return True

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message_received(self, event: AstrMessageEvent):
        """捕获用户消息，暂存以等待机器人回复时配对。"""
        if not self.config.get("enable", True):
            return
        if not self._should_record_session(event):
            return
        record_settings = self.config.get("record_settings", {}) or {}
        if not record_settings.get("record_user_messages", True):
            return
        text = _safe_text(
            event.message_str,
            max_len=int(record_settings.get("max_message_length", 2000) or 2000),
        )
        if not self._should_capture_message(text):
            return
        session_id = event.session_id or event.unified_msg_origin or "default"
        self._pending_pairs[session_id] = {
            "user": text,
            "ts": time.time(),
            "sender": self._get_sender_display(event),
            "umo": event.unified_msg_origin or "",
            "is_group": bool(getattr(event.message_obj, "group_id", "") if event.message_obj else False),
        }

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """LLM 请求前，检索相关历史并注入到 system prompt。"""
        if not self.config.get("enable", True):
            return
        retrieve_settings = self.config.get("retrieve_settings", {}) or {}
        if not retrieve_settings.get("inject_to_llm", True):
            return
        user_text = _safe_text(event.message_str)
        min_q = int(retrieve_settings.get("min_query_length", 2) or 0)
        if len(user_text) < min_q:
            return
        top_k = int(retrieve_settings.get("retrieve_top_k", 3) or 3)
        try:
            results = await self._retrieve_memory(user_text, top_k=top_k)
        except Exception as e:
            logger.debug(f"[SharedChatMemory] 检索异常: {e}")
            return
        if not results:
            return

        # 过滤相关度过低的
        threshold = float(retrieve_settings.get("score_threshold", 0.0) or 0.0)
        filtered = []
        for r in results:
            score = r.get("score")
            if score is None:
                filtered.append(r)
                continue
            try:
                if float(score) >= threshold:
                    filtered.append(r)
            except (TypeError, ValueError):
                filtered.append(r)
        if not filtered:
            return

        memory_lines = []
        for idx, r in enumerate(filtered, 1):
            content = (r.get("content") or "").strip()
            if not content:
                continue
            memory_lines.append(f"【片段{idx}】\n{content}")
        if not memory_lines:
            return

        memories_text = "\n\n".join(memory_lines)
        template = retrieve_settings.get(
            "system_prompt_template",
            "以下是从跨会话共享记忆中检索到的历史对话片段，请结合这些信息回答当前用户的问题：\n\n{memories}\n\n请注意：这些片段来自其他用户与本机器人的历史对话，可以用于回答当前用户关于这些话题的提问。若记忆中没有相关信息，请按你的常识回答，不要捏造记忆内容。",
        ) or ""
        injected = template.replace("{memories}", memories_text)

        try:
            if isinstance(req.system_prompt, str) and req.system_prompt:
                req.system_prompt = req.system_prompt + "\n\n" + injected
            else:
                req.system_prompt = injected
        except Exception as e:
            logger.debug(f"[SharedChatMemory] 注入 system_prompt 失败: {e}")
            return

        if self.config.get("debug", False):
            backends = [r.get("backend", "?") for r in filtered]
            logger.debug(f"[SharedChatMemory] 已注入 {len(filtered)} 条记忆（来源: {backends}）。")

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, response: LLMResponse):
        """LLM 回复后，把「用户问 + 机器人答」配对写入记忆库。"""
        if not self.config.get("enable", True):
            return
        if not self._should_record_session(event):
            return

        record_settings = self.config.get("record_settings", {}) or {}
        if not (record_settings.get("record_user_messages", True) or record_settings.get("record_bot_replies", True)):
            return

        session_id = event.session_id or event.unified_msg_origin or "default"
        pending = self._pending_pairs.pop(session_id, None)

        bot_text = _safe_text(
            getattr(response, "completion_text", None) or getattr(response, "text", None) or "",
            max_len=int(record_settings.get("max_message_length", 2000) or 2000),
        )

        user_text = ""
        sender_display = ""
        umo = ""
        is_group = False
        if pending is not None:
            user_text = pending.get("user", "") or ""
            sender_display = pending.get("sender", "") or ""
            umo = pending.get("umo", "") or ""
            is_group = bool(pending.get("is_group", False))
        else:
            user_text = _safe_text(
                event.message_str,
                max_len=int(record_settings.get("max_message_length", 2000) or 2000),
            )

        if record_settings.get("record_user_messages", True) and not self._should_capture_message(user_text):
            user_text = ""
        if record_settings.get("record_bot_replies", True) and not self._should_capture_message(bot_text):
            bot_text = ""

        if not user_text and not bot_text:
            return

        save_pair = record_settings.get("save_pair", True)
        anonymize = self.config.get("privacy", {}).get("anonymize_senders", False)

        meta: Dict[str, Any] = {
            "sender": "(匿名)" if anonymize else (sender_display or "(未知用户)"),
            "platform": umo.split(":", 1)[0] if umo else "",
            "session_type": "group" if is_group else "private",
            "ts": _now_str(),
            "source": _DOC_SOURCE_TAG,
        }

        if save_pair:
            parts = []
            if user_text:
                parts.append(f"用户: {user_text}")
            if bot_text:
                parts.append(f"机器人: {bot_text}")
            content = "\n".join(parts)
            doc_name = f"{_DOC_NAME_PREFIX}{_now_str()}_{uuid.uuid4().hex[:8]}"
            async with self._write_lock:
                await self._store_memory(content, doc_name, meta)
        else:
            ts = _now_str()
            uid = uuid.uuid4().hex[:8]
            if user_text:
                async with self._write_lock:
                    await self._store_memory(
                        f"用户: {user_text}",
                        f"{_DOC_NAME_PREFIX}u_{ts}_{uid}",
                        meta,
                    )
            if bot_text:
                async with self._write_lock:
                    await self._store_memory(
                        f"机器人: {bot_text}",
                        f"{_DOC_NAME_PREFIX}b_{ts}_{uuid.uuid4().hex[:8]}",
                        meta,
                    )

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    def _get_sender_display(self, event: AstrMessageEvent) -> str:
        try:
            name = event.get_sender_name() or ""
        except Exception:
            name = ""
        sender_id = ""
        try:
            msg_obj = event.message_obj
            if msg_obj is not None and getattr(msg_obj, "sender", None) is not None:
                sender_id = str(
                    getattr(msg_obj.sender, "user_id", "")
                    or getattr(msg_obj.sender, "id", "")
                    or ""
                )
        except Exception:
            sender_id = ""
        if name and sender_id:
            return f"{name}({sender_id})"
        return name or sender_id or "(未知用户)"

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        try:
            umo = event.unified_msg_origin or ""
            sender_id = ""
            if event.message_obj is not None and event.message_obj.sender is not None:
                sender_id = str(
                    getattr(event.message_obj.sender, "user_id", "")
                    or getattr(event.message_obj.sender, "id", "")
                    or ""
                )
            admins = getattr(self.context, "admins_id", []) or []
            if isinstance(admins, list):
                if sender_id and sender_id in [str(a) for a in admins]:
                    return True
                if umo and any(str(a) in umo for a in admins):
                    return True
        except Exception:
            pass
        return False

    # ------------------------------------------------------------------
    # 用户指令
    # ------------------------------------------------------------------

    @filter.command("shared_memory_status")
    async def cmd_status(self, event: AstrMessageEvent):
        '''查看共享记忆插件当前状态'''
        enabled = self.config.get("enable", True)
        backend_cfg = self.config.get("backend", {}) or {}
        record_settings = self.config.get("record_settings", {}) or {}
        retrieve_settings = self.config.get("retrieve_settings", {}) or {}

        backends = []
        if backend_cfg.get("use_local", True) and self._local_backend is not None:
            count = await asyncio.to_thread(self._local_backend.count)
            backends.append(f"本地 SQLite+BM25 ({count} 条记忆)")
        if backend_cfg.get("use_local_vector", False) and self._vector_backend is not None:
            count = await asyncio.to_thread(self._vector_backend.count)
            backends.append(f"本地 SQLite+Embedding 向量检索 ({count} 条记忆)")
        if backend_cfg.get("use_astrbot_kb", False) and self._kb_backend is not None:
            kb_ids = await self._kb_backend.resolve_target_kb_ids()
            backends.append(f"AstrBot 知识库 API ({', '.join(kb_ids) if kb_ids else '未配置'})")

        lines = [
            "===== 共享聊天记忆插件状态 =====",
            f"插件版本: v1.2.0",
            f"启用状态: {'✅ 已启用' if enabled else '❌ 已禁用'}",
            f"已启用后端: {', '.join(backends) if backends else '❌ 无'}",
        ]
        lines.append("---- 记录设置 ----")
        lines.append(f"  记录用户消息: {record_settings.get('record_user_messages', True)}")
        lines.append(f"  记录机器人回复: {record_settings.get('record_bot_replies', True)}")
        lines.append(f"  记录私聊: {record_settings.get('record_private_chat', True)}")
        lines.append(f"  记录群聊: {record_settings.get('record_group_chat', True)}")
        lines.append(f"  对话对保存: {record_settings.get('save_pair', True)}")
        lines.append(f"  本地库容量上限: {record_settings.get('max_records', 10000)}")
        lines.append("---- 召回设置 ----")
        lines.append(f"  自动注入 LLM: {retrieve_settings.get('inject_to_llm', True)}")
        lines.append(f"  召回条数 top_k: {retrieve_settings.get('retrieve_top_k', 3)}")
        lines.append(f"  相关度阈值: {retrieve_settings.get('score_threshold', 0.3)}")
        lines.append("================================")
        yield event.plain_result("\n".join(lines))

    @filter.command("shared_memory_search")
    async def cmd_search(self, event: AstrMessageEvent, query: str = ""):
        '''手动检索共享记忆库。用法: /shared_memory_search <关键词>'''
        if not query.strip():
            yield event.plain_result("请提供检索关键词。用法：/shared_memory_search <关键词>")
            return
        if not self.config.get("enable", True):
            yield event.plain_result("插件当前处于禁用状态。")
            return
        top_k = int(self.config.get("retrieve_settings", {}).get("retrieve_top_k", 3) or 3)
        try:
            results = await self._retrieve_memory(query.strip(), top_k=top_k)
        except Exception as e:
            yield event.plain_result(f"检索失败: {e}")
            return
        if not results:
            yield event.plain_result(f"未在共享记忆库中找到与「{query}」相关的内容。")
            return
        lines = [f"===== 共享记忆检索结果（{len(results)} 条）====="]
        for idx, r in enumerate(results, 1):
            content = (r.get("content") or "").strip()
            score = r.get("score")
            backend = r.get("backend", "?")
            score_str = f" | 分数: {score:.3f}" if isinstance(score, (int, float)) else ""
            lines.append(f"【{idx}{score_str} | 来源: {backend}】")
            lines.append(content)
            lines.append("")
        lines.append("================================")
        yield event.plain_result("\n".join(lines))

    @filter.command("shared_memory_clear")
    async def cmd_clear(self, event: AstrMessageEvent):
        '''清空共享记忆库中由本插件写入的所有记忆。仅管理员可用。'''
        if not self._is_admin(event):
            yield event.plain_result("该指令仅管理员可用。")
            return

        results = []
        # 清本地 BM25 后端
        if self._local_backend is not None:
            try:
                n = await asyncio.to_thread(self._local_backend.clear)
                results.append(f"本地 SQLite+BM25: 已清空 {n} 条")
            except Exception as e:
                results.append(f"本地 SQLite+BM25: 清空失败 ({e})")

        # 清本地向量后端
        if self._vector_backend is not None:
            try:
                n = await asyncio.to_thread(self._vector_backend.clear)
                results.append(f"本地 SQLite+Embedding 向量检索: 已清空 {n} 条")
            except Exception as e:
                results.append(f"本地 SQLite+Embedding 向量检索: 清空失败 ({e})")

        # 清 KB 后端
        if self._kb_backend is not None:
            try:
                n = await self._kb_backend.clear()
                if n > 0:
                    results.append(f"AstrBot 知识库 API: 已向 {n} 个知识库发出清理指令")
                else:
                    results.append(
                        "AstrBot 知识库 API: 未能自动清理，请到 WebUI 知识库页面手动删除以 "
                        f"'{_DOC_NAME_PREFIX}' 开头的文档"
                    )
            except Exception as e:
                results.append(f"AstrBot 知识库 API: 清空失败 ({e})")

        if not results:
            yield event.plain_result("当前没有启用的存储后端，无需清理。")
        else:
            yield event.plain_result("清理结果：\n" + "\n".join(results))

    @filter.command("shared_memory_reload")
    async def cmd_reload(self, event: AstrMessageEvent):
        '''重新初始化后端与配置，适用于在 WebUI 修改配置后立即生效。仅管理员可用。'''
        if not self._is_admin(event):
            yield event.plain_result("该指令仅管理员可用。")
            return
        # 关闭旧后端
        if self._local_backend is not None:
            self._local_backend.close()
            self._local_backend = None
        if self._vector_backend is not None:
            await self._vector_backend.close()
            self._vector_backend = None
        if self._kb_backend is not None:
            self._kb_backend.reset_cache()
        # 重新初始化
        self._init_backends()
        # 预解析 KB
        if self._kb_backend is not None:
            try:
                kb_ids = await self._kb_backend.resolve_target_kb_ids()
                kb_info = f"，知识库: {kb_ids}" if kb_ids else ""
            except Exception:
                kb_info = ""
        else:
            kb_info = ""
        backends = []
        if self._local_backend is not None:
            backends.append("本地 SQLite+BM25")
        if self._vector_backend is not None:
            backends.append("本地 SQLite+Embedding 向量检索")
        if self._kb_backend is not None:
            backends.append("AstrBot 知识库 API")
        yield event.plain_result(
            f"已重新初始化后端。当前启用: {', '.join(backends) if backends else '无'}{kb_info}"
        )
