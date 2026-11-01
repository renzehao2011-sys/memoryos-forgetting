import json
import os
import numpy as np
from collections import defaultdict
import faiss
import heapq
import threading
from datetime import datetime

try:
    from .utils import (
        get_timestamp, generate_id, get_embedding, normalize_vector, 
        compute_time_decay, ensure_directory_exists, OpenAIClient
    )
    from .schema import migrate_mid_term
except ImportError:
    from utils import (
        get_timestamp, generate_id, get_embedding, normalize_vector, 
        compute_time_decay, ensure_directory_exists, OpenAIClient
    )
    from schema import migrate_mid_term

# Heat computation constants (can be tuned or made configurable)
HEAT_ALPHA = 1.0
HEAT_BETA = 1.0
HEAT_GAMMA = 1
RECENCY_TAU_HOURS = 24 # For R_recency calculation in compute_segment_heat

def compute_segment_heat(session, alpha=HEAT_ALPHA, beta=HEAT_BETA, gamma=HEAT_GAMMA, tau_hours=RECENCY_TAU_HOURS):
    N_visit = session.get("N_visit", 0)
    L_interaction = session.get("L_interaction", 0)
    
    # Calculate recency based on last_visit_time
    R_recency = 1.0 # Default if no last_visit_time
    if session.get("last_visit_time"):
        R_recency = compute_time_decay(session["last_visit_time"], get_timestamp(), tau_hours)
    
    session["R_recency"] = R_recency # Update session's recency factor
    return alpha * N_visit + beta * L_interaction + gamma * R_recency

class MidTermMemory:
    def __init__(self, file_path: str, client: OpenAIClient, max_capacity=2000, embedding_model_name: str = "all-MiniLM-L6-v2", embedding_model_kwargs: dict = None):
        self.file_path = file_path
        ensure_directory_exists(self.file_path)
        self.client = client
        self.max_capacity = max_capacity
        self.sessions = {} # {session_id: session_object}
        self.access_frequency = defaultdict(int) # {session_id: access_count_for_lfu}
        self.heap = []  # Min-heap storing (-H_segment, session_id) for hottest segments

        self.embedding_model_name = embedding_model_name
        self.embedding_model_kwargs = embedding_model_kwargs if embedding_model_kwargs is not None else {}
        self.lock = threading.Lock()
        # M7-5：load 失败时置 True，save() 拒绝写盘，避免「读失败→空库→覆盖磁盘」
        self._load_failed = False
        self.load()

    def get_page_by_id(self, page_id):
        for session in self.sessions.values():
            for page in session.get("details", []):
                if page.get("page_id") == page_id:
                    return page
        return None

    def update_page_connections(self, prev_page_id, next_page_id):
        if prev_page_id:
            prev_page = self.get_page_by_id(prev_page_id)
            if prev_page:
                prev_page["next_page"] = next_page_id
        if next_page_id:
            next_page = self.get_page_by_id(next_page_id)
            if next_page:
                next_page["pre_page"] = prev_page_id
        # self.save() # Avoid saving on every minor update; save at higher level operations

    def evict_lfu(self):
        """容量超限时的处置：**不删除**，只打标记（计划单 §4 / §5.3）。

        M5 之前这里是 `self.sessions.pop(lfu_sid)`，属于自动流程中的硬删除，
        违反计划单「不许自动硬删除任何记忆」「没有 delete 字段，删除一律是
        state=archived」。现改为：
          1. 选出 LFU 最低、且尚未标记过的会话；
          2. 打 `over_capacity_since` / `over_capacity_reason` 标记；
          3. 从热段堆里摘掉（M7-3：rebuild_heap 也会跳过已标记会话，所以是持久的），但仍留在 sessions 与
             access_frequency 里，页面一条不删；
          4. **不写 state** —— state 的唯一权威是整理层
             `consolidate.recompute_states`（§5.3：分数 < 0.05 且 90 天未召回）。
        M7-3 修正：整理层（consolidate）并不读这个标记，它只出现在整理报告的
        `over_capacity_total` 和 weekly_spotcheck.py 的 C 栏。处置权在人：扩容后调
        `clear_over_capacity()` 放回堆里，或走 `--confirm-archive` 归档。

        若这些会话的分数仍然高，说明容量该扩，人会在抽检报告里看到并决定。

        返回 True 表示本轮标记了一条，False 表示没有可标记对象。
        """
        if not self.sessions:
            return False

        candidates = [
            (self.access_frequency.get(sid, 0), sid)
            for sid in self.sessions
            if not self.sessions[sid].get("over_capacity_since")
        ]
        if not candidates:
            print("MidTermMemory: 全部会话均已标记 over_capacity，本轮不再标记（仍不删除）。")
            return False

        count, lfu_sid = min(candidates)
        session = self.sessions[lfu_sid]
        session.setdefault("state", "hot")
        session["over_capacity_since"] = get_timestamp()
        session["over_capacity_reason"] = (
            f"会话数 {len(self.sessions)} > max_capacity {self.max_capacity}，"
            f"LFU 最低（{count} 次）待人工处置，不删除（计划单 §5.3）"
        )

        # 只从热段堆摘掉，不动 sessions / access_frequency / details
        self.heap = [(h, sid) for (h, sid) in self.heap if sid != lfu_sid]
        heapq.heapify(self.heap)

        n = len(self.sessions)
        print(f"MidTermMemory: 容量超限 {n}/{self.max_capacity}，"
              f"会话 {lfu_sid}（LFU={count}）标记 over_capacity，等待整理层判定，未删除。")
        if n > 2 * self.max_capacity:
            print(f"MidTermMemory: 警告——会话数已达上限的 {n / self.max_capacity:.1f} 倍，"
                  f"存储会持续增长；请人工评估扩容 max_capacity 或归档（本模块不自动删除）。")
        self.save()
        return True

    def add_session(self, summary, details, summary_keywords=None):
        session_id = generate_id("session")
        summary_vec = get_embedding(
            summary, 
            model_name=self.embedding_model_name, 
            **self.embedding_model_kwargs
        )
        summary_vec = normalize_vector(summary_vec).tolist()
        summary_keywords = summary_keywords if summary_keywords is not None else []
        
        processed_details = []
        for page_data in details:
            page_id = page_data.get("page_id", generate_id("page"))
            
            # 检查是否已有embedding，避免重复计算
            if "page_embedding" in page_data and page_data["page_embedding"]:
                print(f"MidTermMemory: Reusing existing embedding for page {page_id}")
                inp_vec = page_data["page_embedding"]
                # 确保embedding是normalized的
                if isinstance(inp_vec, list):
                    inp_vec_np = np.array(inp_vec, dtype=np.float32)
                    if np.linalg.norm(inp_vec_np) > 1.1 or np.linalg.norm(inp_vec_np) < 0.9:  # 检查是否需要重新normalize
                        inp_vec = normalize_vector(inp_vec_np).tolist()
            else:
                print(f"MidTermMemory: Computing new embedding for page {page_id}")
                full_text = f"User: {page_data.get('user_input','')} Assistant: {page_data.get('agent_response','')}"
                inp_vec = get_embedding(
                    full_text,
                    model_name=self.embedding_model_name,
                    **self.embedding_model_kwargs
                )
                inp_vec = normalize_vector(inp_vec).tolist()
            
            # 使用已有keywords或设置为空（由multi-summary提供）
            if "page_keywords" in page_data and page_data["page_keywords"]:
                print(f"MidTermMemory: Using existing keywords for page {page_id}")
                page_keywords = page_data["page_keywords"]
            else:
                print(f"MidTermMemory: Setting empty keywords for page {page_id} (will be filled by multi-summary)")
                page_keywords = []
            
            processed_page = {
                **page_data, # Carry over existing fields like user_input, agent_response, timestamp
                "page_id": page_id,
                "page_embedding": inp_vec,
                "page_keywords": page_keywords,
                "preloaded": page_data.get("preloaded", False), # Preserve if passed
                "analyzed": page_data.get("analyzed", False),   # Preserve if passed
                # pre_page, next_page, meta_info are handled by DynamicUpdater
            }
            processed_details.append(processed_page)
        
        current_ts = get_timestamp()
        session_obj = {
            "id": session_id,
            "summary": summary,
            "summary_keywords": summary_keywords,
            "summary_embedding": summary_vec,
            "details": processed_details,
            "L_interaction": len(processed_details),
            "R_recency": 1.0, # Initial recency
            "N_visit": 0,
            "H_segment": 0.0, # Initial heat, will be computed
            "timestamp": current_ts, # Creation timestamp
            "last_visit_time": current_ts, # Also initial last_visit_time for recency calc
            "access_count_lfu": 0 # For LFU eviction policy
        }
        session_obj["H_segment"] = compute_segment_heat(session_obj)
        self.sessions[session_id] = session_obj
        self.access_frequency[session_id] = 0 # Initialize for LFU
        heapq.heappush(self.heap, (-session_obj["H_segment"], session_id)) # Use negative heat for max-heap behavior
        
        print(f"MidTermMemory: Added new session {session_id}. Initial heat: {session_obj['H_segment']:.2f}.")
        if len(self.sessions) > self.max_capacity:
            self.evict_lfu()
        self.save()
        return session_id

    def rebuild_heap(self):
        self.heap = []
        for sid, session_data in self.sessions.items():
            # M7-3：over_capacity 的会话不再进热段堆 —— 否则「摘除」只是暂时的：
            # 每次 search_sessions / insert_pages 都会调本函数，一调就又把它加回来，
            # 于是 evict_lfu 里「不再触发画像升级」的说法根本不成立。
            # 解除标记的唯一途径是人工 clear_over_capacity()（扩容后重新纳入）。
            if session_data.get("over_capacity_since"):
                continue
            # Ensure H_segment is up-to-date before rebuilding heap if necessary
            # session_data["H_segment"] = compute_segment_heat(session_data)
            heapq.heappush(self.heap, (-session_data["H_segment"], sid))
        # heapq.heapify(self.heap) # Not needed if pushing one by one
        # No save here, it's an internal operation often followed by other ops that save

    def clear_over_capacity(self, ids=None):
        """M7-3：人工解除 over_capacity 标记，让会话重新参与热段调度。

        ids 为空时解除全部。返回解除条数。只清标记，不动 state / details。
        典型用法：确认扩容 max_capacity 之后，把上一轮被摘出堆的会话放回来。
        """
        wanted = None if not ids else set(str(x) for x in ids)
        cleared = 0
        for sid, session in self.sessions.items():
            if not session.get("over_capacity_since"):
                continue
            if wanted is not None and sid not in wanted:
                continue
            session.pop("over_capacity_since", None)
            session.pop("over_capacity_reason", None)
            cleared += 1
        if cleared:
            self.rebuild_heap()
            self.save()
            print(f"MidTermMemory: 解除 {cleared} 个会话的 over_capacity 标记，已重新纳入热段堆。")
        return cleared

    def insert_pages_into_session(self, summary_for_new_pages, keywords_for_new_pages, pages_to_insert, 
                                  similarity_threshold=0.6, keyword_similarity_alpha=1.0):
        if not self.sessions: # If no existing sessions, just add as a new one
            print("MidTermMemory: No existing sessions. Adding new session directly.")
            return self.add_session(summary_for_new_pages, pages_to_insert, keywords_for_new_pages)

        new_summary_vec = get_embedding(
            summary_for_new_pages,
            model_name=self.embedding_model_name,
            **self.embedding_model_kwargs
        )
        new_summary_vec = normalize_vector(new_summary_vec)
        
        best_sid = None
        best_overall_score = -1

        for sid, existing_session in self.sessions.items():
            existing_summary_vec = np.array(existing_session["summary_embedding"], dtype=np.float32)
            semantic_sim = float(np.dot(existing_summary_vec, new_summary_vec))
            
            # Keyword similarity (Jaccard index based)
            existing_keywords = set(existing_session.get("summary_keywords", []))
            new_keywords_set = set(keywords_for_new_pages)
            s_topic_keywords = 0
            if existing_keywords and new_keywords_set:
                intersection = len(existing_keywords.intersection(new_keywords_set))
                union = len(existing_keywords.union(new_keywords_set))
                if union > 0:
                    s_topic_keywords = intersection / union 
            
            overall_score = semantic_sim + keyword_similarity_alpha * s_topic_keywords
            
            if overall_score > best_overall_score:
                best_overall_score = overall_score
                best_sid = sid
        
        if best_sid and best_overall_score >= similarity_threshold:
            print(f"MidTermMemory: Merging pages into session {best_sid}. Score: {best_overall_score:.2f} (Threshold: {similarity_threshold})")
            target_session = self.sessions[best_sid]
            
            processed_new_pages = []
            for page_data in pages_to_insert:
                page_id = page_data.get("page_id", generate_id("page")) # Use existing or generate new ID
                
                # 检查是否已有embedding，避免重复计算
                if "page_embedding" in page_data and page_data["page_embedding"]:
                    print(f"MidTermMemory: Reusing existing embedding for page {page_id}")
                    inp_vec = page_data["page_embedding"]
                    # 确保embedding是normalized的
                    if isinstance(inp_vec, list):
                        inp_vec_np = np.array(inp_vec, dtype=np.float32)
                        if np.linalg.norm(inp_vec_np) > 1.1 or np.linalg.norm(inp_vec_np) < 0.9:  # 检查是否需要重新normalize
                            inp_vec = normalize_vector(inp_vec_np).tolist()
                else:
                    print(f"MidTermMemory: Computing new embedding for page {page_id}")
                    full_text = f"User: {page_data.get('user_input','')} Assistant: {page_data.get('agent_response','')}"
                    inp_vec = get_embedding(
                        full_text,
                        model_name=self.embedding_model_name,
                        **self.embedding_model_kwargs
                    )
                    inp_vec = normalize_vector(inp_vec).tolist()
                
                # 使用已有keywords或继承session的keywords
                if "page_keywords" in page_data and page_data["page_keywords"]:
                    print(f"MidTermMemory: Using existing keywords for page {page_id}")
                    page_keywords_current = page_data["page_keywords"]
                else:
                    print(f"MidTermMemory: Using session keywords for page {page_id}")
                    page_keywords_current = keywords_for_new_pages

                processed_page = {
                    **page_data, # Carry over existing fields
                    "page_id": page_id,
                    "page_embedding": inp_vec,
                    "page_keywords": page_keywords_current,
                    # analyzed, preloaded flags should be part of page_data if set
                }
                target_session["details"].append(processed_page)
                processed_new_pages.append(processed_page)

            target_session["L_interaction"] += len(pages_to_insert)
            target_session["last_visit_time"] = get_timestamp() # Update last visit time on modification
            target_session["H_segment"] = compute_segment_heat(target_session)
            self.rebuild_heap() # Rebuild heap as heat has changed
            self.save()
            return best_sid
        else:
            print(f"MidTermMemory: No suitable session to merge (best score {best_overall_score:.2f} < threshold {similarity_threshold}). Creating new session.")
            return self.add_session(summary_for_new_pages, pages_to_insert, keywords_for_new_pages)

    def search_sessions(self, query_text, segment_similarity_threshold=0.0, page_similarity_threshold=0.0, 
                          top_k_sessions=6, keyword_alpha=1.0, recency_tau_search=3600):
        if not self.sessions:
            return []

        query_vec = get_embedding(
            query_text,
            model_name=self.embedding_model_name,
            **self.embedding_model_kwargs
        )
        query_vec = normalize_vector(query_vec)
        query_keywords = set()  # Keywords extraction removed, relying on semantic similarity

        candidate_sessions = []
        session_ids = list(self.sessions.keys())
        if not session_ids: return []

        summary_embeddings_list = [self.sessions[s]["summary_embedding"] for s in session_ids]
        summary_embeddings_np = np.array(summary_embeddings_list, dtype=np.float32)

        dim = summary_embeddings_np.shape[1]
        index = faiss.IndexFlatIP(dim) # Inner product for similarity
        index.add(summary_embeddings_np)
        
        query_arr_np = np.array([query_vec], dtype=np.float32)
        distances, indices = index.search(query_arr_np, min(top_k_sessions, len(session_ids)))

        results = []
        current_time_str = get_timestamp()

        for i, idx in enumerate(indices[0]):
            if idx == -1: continue
            
            session_id = session_ids[idx]
            session = self.sessions[session_id]
            semantic_sim_score = float(distances[0][i]) # This is the dot product

            # Keyword similarity for session summary
            session_keywords = set(session.get("summary_keywords", []))
            s_topic_keywords = 0
            if query_keywords and session_keywords:
                intersection = len(query_keywords.intersection(session_keywords))
                union = len(query_keywords.union(session_keywords))
                if union > 0: s_topic_keywords = intersection / union
            
            # Time decay for session recency in search scoring
            # time_decay_factor = compute_time_decay(session["timestamp"], current_time_str, tau_hours=recency_tau_search)
            
            # Combined score for session relevance
            session_relevance_score =  (semantic_sim_score + keyword_alpha * s_topic_keywords)

            if session_relevance_score >= segment_similarity_threshold:
                matched_pages_in_session = []
                for page in session.get("details", []):
                    page_embedding = np.array(page["page_embedding"], dtype=np.float32)
                    # page_keywords = set(page.get("page_keywords", []))
                    
                    page_sim_score = float(np.dot(page_embedding, query_vec))
                    # Can also add keyword sim for pages if needed, but keeping it simpler for now

                    if page_sim_score >= page_similarity_threshold:
                        matched_pages_in_session.append({"page_data": page, "score": page_sim_score})
                
                if matched_pages_in_session:
                    # Update session access stats
                    session["N_visit"] += 1
                    session["last_visit_time"] = current_time_str
                    session["access_count_lfu"] = session.get("access_count_lfu", 0) + 1
                    self.access_frequency[session_id] = session["access_count_lfu"]
                    session["H_segment"] = compute_segment_heat(session)
                    self.rebuild_heap() # Heat changed
                    
                    results.append({
                        "session_id": session_id,
                        "session_summary": session["summary"],
                        "session_relevance_score": session_relevance_score,
                        "matched_pages": sorted(matched_pages_in_session, key=lambda x: x["score"], reverse=True) # Sort pages by score
                    })
        
        self.save() # Save changes from access updates
        # Sort final results by session_relevance_score
        return sorted(results, key=lambda x: x["session_relevance_score"], reverse=True)

    def save(self):
        # Make a copy for saving to avoid modifying heap during iteration if it happens
        # Though current heap is list of tuples, so direct modification risk is low
        # sessions_to_save = {sid: data for sid, data in self.sessions.items()}
        data_to_save = {
            "sessions": self.sessions,
            "access_frequency": dict(self.access_frequency), # Convert defaultdict to dict for JSON
            # Heap is derived, no need to save typically, but can if desired for faster load
            # "heap_snapshot": self.heap 
        }
        if getattr(self, "_load_failed", False):
            # M7-5：读盘失败后禁止写盘，否则空 sessions 会把磁盘上的真实记忆冲掉。
            print(f"MidTermMemory: 上次 load 失败（_load_failed=True），拒绝写盘以保护 {self.file_path}。")
            return
        try:
            with self.lock:
                # M7-5：原子写（tmp + os.replace），避免 proot 被中断时留下截断文件
                tmp = self.file_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data_to_save, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.file_path)
        except IOError as e:
            print(f"Error saving MidTermMemory to {self.file_path}: {e}")

    def load(self):
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.sessions = data.get("sessions", {})
                self.access_frequency = defaultdict(int, data.get("access_frequency", {}))
                # M1: 字段迁移（补 12 个标准字段，幂等，不改既有逻辑）
                _mig_stats = migrate_mid_term(data)
                if _mig_stats.get("fields_added"):
                    print(f"MidTermMemory: M1 schema migration added {_mig_stats['fields_added']} field(s) "
                          f"across {_mig_stats['sessions']} session(s) / {_mig_stats['pages']} page(s).")
                self.rebuild_heap() # Rebuild heap from loaded sessions
            print(f"MidTermMemory: Loaded from {self.file_path}. Sessions: {len(self.sessions)}.")
        except FileNotFoundError:
            print(f"MidTermMemory: No history file found at {self.file_path}. Initializing new memory.")
        except json.JSONDecodeError:
            # M7-5：损坏文件改名保留（不删），并禁止后续写盘
            self._load_failed = True
            self._quarantine("JSON 解析失败")
        except Exception as e:
            self._load_failed = True
            print(f"MidTermMemory: 读取 {self.file_path} 时发生异常（{e}）；已禁止写盘，本次以空记忆运行，请人工检查。")

    def _quarantine(self, why=""):
        """M7-5：把损坏文件改名保留（不删除），供人工恢复。"""
        try:
            stamp = get_timestamp().replace(":", "").replace(" ", "_")
            bad = f"{self.file_path}.corrupt-{stamp}"
            os.replace(self.file_path, bad)
            print(f"MidTermMemory: {why}，原文件已保留为 {bad}（未删除）；已禁止写盘（_load_failed=True）。")
        except OSError as e:
            print(f"MidTermMemory: {why}，且改名保留失败（{e}）；本次不写盘，请人工处理。") 