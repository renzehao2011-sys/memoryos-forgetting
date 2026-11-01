import json
import os
import numpy as np
import faiss
from collections import deque
import threading
try:
    from .utils import get_timestamp, get_embedding, normalize_vector, ensure_directory_exists
    from .schema import migrate_long_term, DEFAULT_IMPORTANCE, DEFAULT_EMOTION
    from .schema import _stable_id as _stable_knowledge_id
except ImportError:
    from utils import get_timestamp, get_embedding, normalize_vector, ensure_directory_exists
    from schema import migrate_long_term, DEFAULT_IMPORTANCE, DEFAULT_EMOTION
    from schema import _stable_id as _stable_knowledge_id

class LongTermMemory:
    def __init__(self, file_path, knowledge_capacity=100, embedding_model_name: str = "all-MiniLM-L6-v2", embedding_model_kwargs: dict = None):
        self.file_path = file_path
        ensure_directory_exists(self.file_path)
        self.knowledge_capacity = knowledge_capacity
        self.user_profiles = {} # {user_id: {data: "profile_string", "last_updated": "timestamp"}}
        # M7-1：**不再用 deque(maxlen=N)**。
        # 原实现 append 超过 N 条时，deque 会静默丢掉最旧一条，而 save() 写的正是
        # list(self.knowledge_base) —— 等于在自动流程里硬删除记忆（违反计划单 §4/§12.4）。
        # 现改为无界 deque + 超过 knowledge_capacity 只打 over_capacity 标记（不删、不写 state），
        # 与 mid_term.evict_lfu 的处置口径保持一致。
        self.knowledge_base = deque()      # For general/user private knowledge
        self.assistant_knowledge = deque() # For assistant specific knowledge
        # load() 失败时置 True：save() 见到它就拒绝写盘，避免「读失败 → 空库 → 覆盖磁盘」
        self._load_failed = False

        self.embedding_model_name = embedding_model_name
        self.embedding_model_kwargs = embedding_model_kwargs if embedding_model_kwargs is not None else {}
        self.lock = threading.Lock()
        self.load()

    def update_user_profile(self, user_id, new_data, merge=True):
        if merge and user_id in self.user_profiles and self.user_profiles[user_id].get("data"): # Check if data exists
            current_data = self.user_profiles[user_id]["data"]
            if isinstance(current_data, str) and isinstance(new_data, str):
                updated_data = f"{current_data}\n\n--- Updated on {get_timestamp()} ---\n{new_data}"
            else: # Fallback to overwrite if types are not strings or for more complex merge
                updated_data = new_data
        else:
            # If merge=False or no existing data, replace with new data
            updated_data = new_data
        
        self.user_profiles[user_id] = {
            "data": updated_data,
            "last_updated": get_timestamp()
        }
        print(f"LongTermMemory: Updated user profile for {user_id} (merge={merge}).")
        self.save()

    def get_raw_user_profile(self, user_id):
        return self.user_profiles.get(user_id, {}).get("data", "None") # Return "None" string if not found

    def get_user_profile_data(self, user_id):
        return self.user_profiles.get(user_id, {})

    def add_knowledge_entry(self, knowledge_text, knowledge_deque: deque, type_name="knowledge", score=None):
        if not knowledge_text or knowledge_text.strip().lower() in ["", "none", "- none", "- none."]:
            print(f"LongTermMemory: Empty {type_name} received, not saving.")
            return
        
        # M7-1：deque 已无 maxlen，append 不会再挤掉最旧条目。
        # 容量上限改为「打标记」表达，语义同 mid_term.evict_lfu：不删、不改 state。
        vec = get_embedding(
            knowledge_text, 
            model_name=self.embedding_model_name, 
            **self.embedding_model_kwargs
        )
        vec = normalize_vector(vec).tolist()
        ts = get_timestamp()
        entry = {
            "knowledge": knowledge_text,
            "timestamp": ts,
            "knowledge_embedding": vec
        }
        # M2: 标准字段（写入时即带，不再依赖 load 时迁移）
        entry["id"] = entry.get("id") or _stable_knowledge_id("know", knowledge_text)
        entry["text"] = knowledge_text
        entry["type"] = (score or {}).get("type") or "knowledge"
        entry["importance"] = (score or {}).get("importance", DEFAULT_IMPORTANCE)
        entry["emotion"] = (score or {}).get("emotion", DEFAULT_EMOTION)
        entry["created_at"] = ts
        entry["last_used_at"] = ts
        entry["used_count"] = 0
        entry["state"] = "hot"
        entry["valid_from"] = None
        entry["valid_to"] = None
        entry["source"] = "long_term"
        knowledge_deque.append(entry)
        self._mark_over_capacity(knowledge_deque, type_name)
        print(f"LongTermMemory: Added {type_name}. Current count: {len(knowledge_deque)}.")
        self.save()

    def _mark_over_capacity(self, knowledge_deque, type_name="knowledge"):
        """M7-1：超过 knowledge_capacity 时只打标记，绝不删除。

        与 mid_term.evict_lfu 同一口径：打 `over_capacity_since` / `over_capacity_reason`，
        不动 state（state 的唯一权威是整理层 §5.3），条目仍然留在库里、仍可被召回、
        仍能被审计和回退。人工扩容或清理后可用 `clear_over_capacity()` 解除标记。

        返回本次新标记的条数。
        """
        n = len(knowledge_deque)
        if n <= self.knowledge_capacity:
            return 0
        excess = n - self.knowledge_capacity
        # 幂等关键：excess 是“应该被标记的总数”，不是“本次要新标几个”。
        # 必须扣掉已经标过的，否则每追加一条就会把更多“其实还在容量内”的条目拖下水
        # （实测：cap=5 加到 7 条时，不扣已标数会标 3 条而不是 2 条，且随追加持续放大）。
        already = sum(1 for e in knowledge_deque
                      if isinstance(e, dict) and e.get("over_capacity_since"))
        need = excess - already
        if need <= 0:
            return 0
        ts = get_timestamp()
        marked = 0
        for entry in knowledge_deque:          # deque 顺序 = 写入顺序，从最旧开始标
            if marked >= need:
                break
            if not isinstance(entry, dict) or entry.get("over_capacity_since"):
                continue
            entry["over_capacity_since"] = ts
            entry["over_capacity_reason"] = (
                f"{type_name} 条数 {n} > knowledge_capacity {self.knowledge_capacity}，"
                f"标记待人工处置（计划单 §4/§12.4：不硬删除）"
            )
            marked += 1
        if marked:
            print(f"LongTermMemory: {type_name} 超出容量 {n}/{self.knowledge_capacity}，"
                  f"标记 {marked} 条 over_capacity，未删除任何条目。")
        if n > 2 * self.knowledge_capacity:
            print(f"LongTermMemory: 警告——{type_name} 已达容量上限的 {n / self.knowledge_capacity:.1f} 倍，"
                  f"存储会持续增长；请人工评估扩容 knowledge_capacity 或走归档确认。")
        return marked

    def clear_over_capacity(self, ids=None):
        """M7-1：人工解除 over_capacity 标记（扩容后把条目重新纳入正常流转）。

        ids 为空时解除全部。返回解除条数。只清标记，不动内容与 state。
        """
        wanted = None if not ids else set(str(x) for x in ids)
        cleared = 0
        for deque_ in (self.knowledge_base, self.assistant_knowledge):
            for entry in deque_:
                if not isinstance(entry, dict) or not entry.get("over_capacity_since"):
                    continue
                if wanted is not None and str(entry.get("id")) not in wanted:
                    continue
                entry.pop("over_capacity_since", None)
                entry.pop("over_capacity_reason", None)
                cleared += 1
        if cleared:
            print(f"LongTermMemory: 解除 {cleared} 条 over_capacity 标记。")
            self.save()
        return cleared

    def add_user_knowledge(self, knowledge_text, score=None):
        self.add_knowledge_entry(knowledge_text, self.knowledge_base, "user knowledge", score=score)

    def add_assistant_knowledge(self, knowledge_text, score=None):
        self.add_knowledge_entry(knowledge_text, self.assistant_knowledge, "assistant knowledge", score=score)

    def get_user_knowledge(self):
        return list(self.knowledge_base)

    def get_assistant_knowledge(self):
        return list(self.assistant_knowledge)

    def _search_knowledge_deque(self, query, knowledge_deque: deque, threshold=0.1, top_k=5):
        if not knowledge_deque:
            return []
        
        query_vec = get_embedding(
            query, 
            model_name=self.embedding_model_name, 
            **self.embedding_model_kwargs
        )
        query_vec = normalize_vector(query_vec)
        
        embeddings = []
        valid_entries = []
        for entry in knowledge_deque:
            if "knowledge_embedding" in entry and entry["knowledge_embedding"]:
                embeddings.append(np.array(entry["knowledge_embedding"], dtype=np.float32))
                valid_entries.append(entry)
            else:
                print(f"Warning: Entry without embedding found in knowledge_deque: {entry.get('knowledge','N/A')[:50]}")

        if not embeddings:
            return []
            
        embeddings_np = np.array(embeddings, dtype=np.float32)
        if embeddings_np.ndim == 1: # Single item case
            if embeddings_np.shape[0] == 0: return [] # Empty embeddings
            embeddings_np = embeddings_np.reshape(1, -1)
        
        if embeddings_np.shape[0] == 0: # No valid embeddings
            return []

        dim = embeddings_np.shape[1]
        index = faiss.IndexFlatIP(dim) # Using Inner Product for similarity
        index.add(embeddings_np)
        
        query_arr = np.array([query_vec], dtype=np.float32)
        distances, indices = index.search(query_arr, min(top_k, len(valid_entries))) # Search at most k or length of valid_entries
        
        results = []
        for i, idx in enumerate(indices[0]):
            if idx != -1: # faiss returns -1 for no valid index
                similarity_score = float(distances[0][i]) # For IndexFlatIP, distance is the dot product (similarity)
                if similarity_score >= threshold:
                    results.append(valid_entries[idx]) # Add the original entry dict
        
        # Sort by similarity score descending before returning, as faiss might not guarantee order for IP
        results.sort(key=lambda x: float(np.dot(np.array(x["knowledge_embedding"], dtype=np.float32), query_vec)), reverse=True)
        return results

    def search_user_knowledge(self, query, threshold=0.1, top_k=5):
        results = self._search_knowledge_deque(query, self.knowledge_base, threshold, top_k)
        print(f"LongTermMemory: Searched user knowledge for '{query[:30]}...'. Found {len(results)} matches.")
        return results

    def search_assistant_knowledge(self, query, threshold=0.1, top_k=5):
        results = self._search_knowledge_deque(query, self.assistant_knowledge, threshold, top_k)
        print(f"LongTermMemory: Searched assistant knowledge for '{query[:30]}...'. Found {len(results)} matches.")
        return results

    def save(self):
        if getattr(self, "_load_failed", False):
            # M7-5：读盘失败后不允许用空/半截数据覆盖磁盘。
            print(f"LongTermMemory: 上次 load 失败（_load_failed=True），拒绝写盘以保护 {self.file_path}。"
                  f"请人工检查 .corrupt-* 文件后再重启。")
            return
        data = {
            "user_profiles": self.user_profiles,
            "knowledge_base": list(self.knowledge_base), # Convert deques to lists for JSON serialization
            "assistant_knowledge": list(self.assistant_knowledge)
        }
        try:
            with self.lock:
                # M7-5：原子写（tmp + os.replace）。原来是 open(path,"w") 直写，
                # proot 被中断时会留下截断文件，下次 load 又会被当成空库。
                tmp = self.file_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.file_path)
        except IOError as e:
            print(f"Error saving LongTermMemory to {self.file_path}: {e}")

    def load(self):
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.user_profiles = data.get("user_profiles", {})
                # M1: 字段迁移（补 12 个标准字段，幂等，不改既有逻辑）
                _mig_stats = migrate_long_term(data)
                if _mig_stats.get("fields_added"):
                    print(f"LongTermMemory: M1 schema migration added {_mig_stats['fields_added']} field(s) "
                          f"across {_mig_stats['knowledge']} knowledge entr(ies).")
                # M7-1: 加载时也不能截断——原 deque(kb_data, maxlen=N) 会在文件已有 >N 条时
                # 静默丢掉靠前的条目，等于「打开一次就少几条」。
                kb_data = data.get("knowledge_base", [])
                self.knowledge_base = deque(kb_data)
                if len(self.knowledge_base) > self.knowledge_capacity:
                    print(f"LongTermMemory: 载入 {len(self.knowledge_base)} 条，超过 knowledge_capacity "
                          f"{self.knowledge_capacity}；不截断，按容量超限打标记处理。")
                    self._mark_over_capacity(self.knowledge_base, "user knowledge")

                ak_data = data.get("assistant_knowledge", [])
                self.assistant_knowledge = deque(ak_data)
                if len(self.assistant_knowledge) > self.knowledge_capacity:
                    self._mark_over_capacity(self.assistant_knowledge, "assistant knowledge")

            print(f"LongTermMemory: Loaded from {self.file_path}.")
        except FileNotFoundError:
            print(f"LongTermMemory: No history file found at {self.file_path}. Initializing new memory.")
        except json.JSONDecodeError:
            # M7-5：损坏文件先改名保存（不删除），再以空库启动；
            # 同时置 _load_failed，禁止后续 save() 覆盖磁盘。
            self._load_failed = True
            self._quarantine("JSON 解析失败")
        except Exception as e:
             # 其它异常同样不能当成「空记忆」继续跑，否则下一次 save 就把数据冲掉
             self._load_failed = True
             print(f"LongTermMemory: 读取 {self.file_path} 时发生异常（{e}）；已禁止写盘，"
                   f"请人工检查。本次以空记忆运行。")

    def _quarantine(self, why=""):
        """M7-5：把损坏文件改名保留（不删），供人工恢复。"""
        try:
            stamp = get_timestamp().replace(":", "").replace(" ", "_")
            bad = f"{self.file_path}.corrupt-{stamp}"
            os.replace(self.file_path, bad)
            print(f"LongTermMemory: {why}，原文件已保留为 {bad}（未删除）；"
                  f"本次以空记忆启动，且已禁止写盘（_load_failed=True）。")
        except OSError as e:
            print(f"LongTermMemory: {why}，且改名保留失败（{e}）；本次以空记忆运行但不写盘，请人工处理。") 