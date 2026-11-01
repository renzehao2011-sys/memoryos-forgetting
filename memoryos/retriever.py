from collections import deque
import heapq
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

try:
    from .utils import get_timestamp, OpenAIClient, run_parallel_tasks
    from .short_term import ShortTermMemory
    from .mid_term import MidTermMemory
    from .long_term import LongTermMemory
    from . import scoring as m2_scoring
except ImportError:
    from utils import get_timestamp, OpenAIClient, run_parallel_tasks
    from short_term import ShortTermMemory
    from mid_term import MidTermMemory
    from long_term import LongTermMemory
    import scoring as m2_scoring
# from .updater import Updater # Updater is not directly used by Retriever

class Retriever:
    def __init__(self, 
                 mid_term_memory: MidTermMemory, 
                 long_term_memory: LongTermMemory, 
                 assistant_long_term_memory: Optional[LongTermMemory] = None, # Add assistant LTM
                 # client: OpenAIClient, # Not strictly needed if all LLM calls are within memory modules
                 queue_capacity=7): # Default from main_memoybank was 7 for retrieval_queue
        # Short term memory is usually for direct context, not primary retrieval source here
        # self.short_term_memory = short_term_memory 
        self.mid_term_memory = mid_term_memory
        self.long_term_memory = long_term_memory
        self.assistant_long_term_memory = assistant_long_term_memory # Store assistant LTM reference
        # self.client = client 
        self.retrieval_queue_capacity = queue_capacity
        # 计划单 5.2：分数 < 0.12 默认不召回，但"主动检索"必须能翻出来。
        # 置 True 后 none 档也会返回（标注 recall_tier="none"），供手动查询/整理任务使用。
        self.include_faded = False
        # self.retrieval_queue = deque(maxlen=queue_capacity) # This was instance level, but retrieve returns it, so maybe not needed as instance var

    # ------------------------------------------------------------ M2 三档召回
    def _drop_invalid(self, items, kind=""):
        """P0：踢掉「已失效 / 已归档」的条目。**与 USE_CUSTOM_SCORING 无关。**

        valid_to 非空 = 该事实已被覆盖（合并或被新事实取代）。把已失效的事实
        当成"记忆"返回给模型，是正确性缺陷，不是可以用"降级等价"换来的权衡：
        关掉开关是为了排障，排障时看到的应该是真相，而不是上个月的住址。

        archived 同理（人工确认过该退了）。
        只读：不改条目、不落盘。
        """
        if not items:
            return items
        kept, dropped = [], 0
        for it in items:
            if isinstance(it, dict) and (it.get("valid_to")
                                         or (it.get("state") or "") == "archived"):
                dropped += 1
                continue
            kept.append(it)
        if dropped:
            print(f"Retriever: 跳过 {dropped} 条失效/已归档条目（{kind}；正确性过滤，与打分开关无关）")
        return kept

    def _apply_tiers(self, items, kind, now=None, touch=True):
        """按计划单 5.2 对召回结果做三档裁剪。

        - full : 原样返回完整原文
        - gist : 只返回标题 + 一句话（返回浅拷贝，原文不动）
        - none : 丢弃

        返回的是拷贝列表：不修改存储中的字典，避免把 recall_score 之类写进数据文件。
        USE_CUSTOM_SCORING=0 时原样返回，与 baseline 完全一致。
        """
        if not items:
            return items
        # P0：先做正确性过滤，**与打死分开关无关**。
        items = self._drop_invalid(items, kind)
        if not m2_scoring.is_enabled() or not items:
            return items
        from datetime import datetime
        now = now or datetime.now()
        stamped = []
        for item in items:
            score = m2_scoring.compute_score(item, now)
            tier = m2_scoring.tier_of(score)
            if tier == m2_scoring.TIER_NONE and not self.include_faded:
                continue
            stamped.append((score, tier, item))

        stamped.sort(key=lambda x: x[0], reverse=True)

        out = []
        kept_originals = []
        for score, tier, item in stamped:
            # none 档只被"看见"，不算被使用：不让手动翻档案污染衰减曲线
            if tier != m2_scoring.TIER_NONE:
                kept_originals.append(item)
            picked = dict(item)
            picked["recall_score"] = round(score, 4)
            picked["recall_tier"] = tier
            if tier == m2_scoring.TIER_GIST:
                gist = m2_scoring.make_gist(item, now)
                if kind == "page":
                    picked["user_input"] = gist["text"]
                    picked["agent_response"] = ""
                    picked["meta_info"] = ""
                else:
                    picked["knowledge"] = gist["text"]
            elif tier == m2_scoring.TIER_NONE:
                # 仅主动检索时走到这里：只留"存在过"的痕迹，不给内容
                tag = "【已淡忘 · " + str(item.get("type", "episode")) + "】"
                if kind == "page":
                    picked["user_input"] = tag
                    picked["agent_response"] = ""
                    picked["meta_info"] = ""
                else:
                    picked["knowledge"] = tag
            out.append(picked)

        if touch:
            self._touch_used(kept_originals, now)
        return out

    def _persist_used(self, dirty=None):
        """把 _touch_used 的改动落到磁盘。仅在自定义打分开启时调用。

        dirty：需要落盘的记忆集合，取值 'mid' / 'user' / 'assistant'。
        只写真正被 touch 过的那个文件——原实现每次召回把 4 个文件全写一遍
        （约 246KB JSON），稳定口径下占 13ms。
        """
        if dirty is None:
            dirty = ("mid", "user", "assistant")
        targets = []
        if "mid" in dirty and self.mid_term_memory is not None:
            targets.append(("MidTermMemory", self.mid_term_memory))
        if "user" in dirty and self.long_term_memory is not None:
            targets.append(("LongTermMemory", self.long_term_memory))
        if "assistant" in dirty and self.assistant_long_term_memory is not None:
            targets.append(("AssistantLongTermMemory", self.assistant_long_term_memory))
        for name, mem in targets:
            try:
                mem.save()
            except Exception as e:
                print(f"Retriever: failed to persist usage stats for {name}: {e}")

    def _touch_used(self, items, now=None):
        """被召回即刷新 last_used_at / used_count（计划单第 4 节字段语义）。

        只改内存对象，落盘由各存储层自己负责；USE_CUSTOM_SCORING=0 时完全不碰。
        """
        if not m2_scoring.is_enabled() or not items:
            return False
        try:
            from .utils import get_timestamp as _ts
        except ImportError:
            from utils import get_timestamp as _ts
        stamp = now.strftime(m2_scoring.TS_FMT) if now else _ts()
        touched = False
        for item in items:
            item["last_used_at"] = stamp
            try:
                item["used_count"] = int(item.get("used_count") or 0) + 1
            except (TypeError, ValueError):
                item["used_count"] = 1
            touched = True
        return touched

    def _retrieve_mid_term_context(self, user_query, segment_similarity_threshold, page_similarity_threshold, top_k_sessions):
        """并行任务：从中期记忆检索"""
        print("Retriever: Searching mid-term memory...")
        matched_sessions = self.mid_term_memory.search_sessions(
            query_text=user_query, 
            segment_similarity_threshold=segment_similarity_threshold,
            page_similarity_threshold=page_similarity_threshold,
            top_k_sessions=top_k_sessions
        )
        
        # Use a heap to get top N pages across all relevant sessions based on their scores
        top_pages_heap = []
        page_counter = 0  # Add counter to ensure unique comparison
        for session_match in matched_sessions:
            for page_match in session_match.get("matched_pages", []):
                page_data = page_match["page_data"]
                page_score = page_match["score"] # Using the page score directly
                
                # Add session relevance score to page score or combine them?
                # For now, using page_score. Could be: page_score * session_match["session_relevance_score"]
                combined_score = page_score # Potentially adjust with session_relevance_score

                if len(top_pages_heap) < self.retrieval_queue_capacity:
                    heapq.heappush(top_pages_heap, (combined_score, page_counter, page_data))
                    page_counter += 1
                elif combined_score > top_pages_heap[0][0]: # If current page is better than the worst in heap
                    heapq.heappop(top_pages_heap)
                    heapq.heappush(top_pages_heap, (combined_score, page_counter, page_data))
                    page_counter += 1
        
        # Extract pages from heap, already sorted by heapq property (smallest first)
        # We want highest scores, so either use a max-heap or sort after popping from min-heap.
        retrieved_pages = [item[2] for item in sorted(top_pages_heap, key=lambda x: x[0], reverse=True)]
        print(f"Retriever: Mid-term memory recalled {len(retrieved_pages)} pages.")
        return retrieved_pages

    def _retrieve_user_knowledge(self, user_query, knowledge_threshold, top_k_knowledge):
        """并行任务：从用户长期知识检索"""
        print("Retriever: Searching user long-term knowledge...")
        retrieved_knowledge = self.long_term_memory.search_user_knowledge(
            user_query, threshold=knowledge_threshold, top_k=top_k_knowledge
        )
        print(f"Retriever: Long-term user knowledge recalled {len(retrieved_knowledge)} items.")
        return retrieved_knowledge

    def _retrieve_assistant_knowledge(self, user_query, knowledge_threshold, top_k_knowledge):
        """并行任务：从助手长期知识检索"""
        if not self.assistant_long_term_memory:
            print("Retriever: No assistant long-term memory provided, skipping assistant knowledge retrieval.")
            return []
        
        print("Retriever: Searching assistant long-term knowledge...")
        retrieved_knowledge = self.assistant_long_term_memory.search_assistant_knowledge(
            user_query, threshold=knowledge_threshold, top_k=top_k_knowledge
        )
        print(f"Retriever: Long-term assistant knowledge recalled {len(retrieved_knowledge)} items.")
        return retrieved_knowledge

    def retrieve_context(self, user_query: str, 
                         user_id: str, # Needed for profile, can be used for context filtering if desired
                         segment_similarity_threshold=0.1,  # From main_memoybank example
                         page_similarity_threshold=0.1,     # From main_memoybank example
                         knowledge_threshold=0.01,          # From main_memoybank example
                         top_k_sessions=5,                  # From MidTermMemory search default
                         top_k_knowledge=20                  # Default for knowledge search
                         ):
        print(f"Retriever: Starting PARALLEL retrieval for query: '{user_query[:50]}...'")
        
        # 并行执行三个检索任务
        tasks = [
            lambda: self._retrieve_mid_term_context(user_query, segment_similarity_threshold, page_similarity_threshold, top_k_sessions),
            lambda: self._retrieve_user_knowledge(user_query, knowledge_threshold, top_k_knowledge),
            lambda: self._retrieve_assistant_knowledge(user_query, knowledge_threshold, top_k_knowledge)
        ]
        
        # 使用并行处理
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = []
            for i, task in enumerate(tasks):
                future = executor.submit(task)
                futures.append((i, future))
            
            results = [None] * 3
            for task_idx, future in futures:
                try:
                    results[task_idx] = future.result()
                except Exception as e:
                    print(f"Error in retrieval task {task_idx}: {e}")
                    results[task_idx] = []
        
        retrieved_mid_term_pages, retrieved_user_knowledge, retrieved_assistant_knowledge = results

        # P0：无论开关如何都要过一遍（关掉时只做失效/归档过滤，不动档位）
        retrieved_mid_term_pages = self._apply_tiers(retrieved_mid_term_pages, "page")
        retrieved_user_knowledge = self._apply_tiers(retrieved_user_knowledge, "knowledge")
        retrieved_assistant_knowledge = self._apply_tiers(retrieved_assistant_knowledge, "knowledge")
        if m2_scoring.is_enabled():
            # last_used_at / used_count 已在上一步就地刷新，只落盘被 touch 过的文件。
            # 空列表说明没有条目被 touch，跳过写盘。
            dirty = set()
            if retrieved_mid_term_pages:
                dirty.add("mid")
            if retrieved_user_knowledge:
                dirty.add("user")
            if retrieved_assistant_knowledge:
                dirty.add("assistant")
            if dirty:
                self._persist_used(dirty)

        return {
            "retrieved_pages": retrieved_mid_term_pages or [], # List of page dicts
            "retrieved_user_knowledge": retrieved_user_knowledge or [], # List of knowledge entry dicts
            "retrieved_assistant_knowledge": retrieved_assistant_knowledge or [], # List of assistant knowledge entry dicts
            "retrieved_at": get_timestamp()
        } 