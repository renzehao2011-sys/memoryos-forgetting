import json
import os
from collections import deque
import threading
try:
    from .utils import get_timestamp, ensure_directory_exists
    from .schema import migrate_short_term
except ImportError:
    from utils import get_timestamp, ensure_directory_exists
    from schema import migrate_short_term

class ShortTermMemory:
    def __init__(self, file_path, max_capacity=10):
        self.max_capacity = max_capacity
        self.file_path = file_path
        ensure_directory_exists(self.file_path)
        self.memory = deque(maxlen=max_capacity)
        self.lock = threading.Lock()
        # M7-5：load 失败后禁止写盘，避免空缓冲覆盖磁盘上的真实数据
        self._load_failed = False
        self.load()

    def add_qa_pair(self, qa_pair):
        # Ensure timestamp exists, add if not
        if 'timestamp' not in qa_pair or not qa_pair['timestamp']:
            qa_pair["timestamp"] = get_timestamp()
        
        self.memory.append(qa_pair)
        print(f"ShortTermMemory: Added QA. User: {qa_pair.get('user_input','')[:30]}...")
        self.save()

    def get_all(self):
        return list(self.memory)

    def is_full(self):
        return len(self.memory) >= self.max_capacity # Use >= to be safe

    def pop_oldest(self):
        if self.memory:
            msg = self.memory.popleft()
            print("ShortTermMemory: Evicted oldest QA pair.")
            self.save()
            return msg
        return None

    def save(self):
        if getattr(self, "_load_failed", False):
            # M7-5：读盘失败后拒绝写盘，否则空缓冲会把磁盘数据冲掉
            print(f"ShortTermMemory: 上次 load 失败，拒绝写盘以保护 {self.file_path}。")
            return
        try:
            with self.lock:
                # M7-5：原子写（tmp + os.replace）
                tmp = self.file_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(list(self.memory), f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.file_path)
        except IOError as e:
            print(f"Error saving ShortTermMemory to {self.file_path}: {e}")

    def load(self):
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                # M1: 字段迁移（补 12 个标准字段，幂等，不改既有逻辑）
                if isinstance(data, list):
                    _mig_stats = migrate_short_term(data)
                    if _mig_stats.get("fields_added"):
                        print(f"ShortTermMemory: M1 schema migration added {_mig_stats['fields_added']} field(s) "
                              f"across {_mig_stats['items']} item(s).")
                # Ensure items are loaded correctly, especially if file was empty or malformed
                if isinstance(data, list):
                    self.memory = deque(data, maxlen=self.max_capacity)
                else:
                    self.memory = deque(maxlen=self.max_capacity)
            print(f"ShortTermMemory: Loaded from {self.file_path}.")
        except FileNotFoundError:
            self.memory = deque(maxlen=self.max_capacity)
            print(f"ShortTermMemory: No history file found at {self.file_path}. Initializing new memory.")
        except json.JSONDecodeError:
            # M7-5：损坏文件改名保留（不删除），并禁止后续写盘
            self._load_failed = True
            self.memory = deque(maxlen=self.max_capacity)
            stamp = get_timestamp().replace(":", "").replace(" ", "_")
            bad = f"{self.file_path}.corrupt-{stamp}"
            try:
                os.replace(self.file_path, bad)
                print(f"ShortTermMemory: JSON 解析失败，原文件已保留为 {bad}（未删除），本次不写盘。")
            except OSError as e:
                print(f"ShortTermMemory: JSON 解析失败且改名保留失败（{e}），本次不写盘。")
        except Exception as e:
            self.memory = deque(maxlen=self.max_capacity)
            print(f"ShortTermMemory: An unexpected error occurred during load from {self.file_path}: {e}. Initializing new memory.") 