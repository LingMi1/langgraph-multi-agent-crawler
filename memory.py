"""
SQLite 长期记忆 & URL 去重库 (Phase 2: Long-term Memory)

提供:
  - visited_urls 集合（持久化 URL 去重）
  - agent_sessions 表（会话状态快照）
  - UrlMemory 类：线程安全的 CRUD 操作
  - ★ Phase 3 增强: html_content 缓存列，支持命中记忆时直接返回缓存 HTML
"""

import os
import sqlite3
import threading
import json
from datetime import datetime
from typing import List, Set, Optional, Dict, Any

from schemas import agent_logger

_DEFAULT_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent_memory.db")

_MEMORY_LOCK = threading.Lock()


class UrlMemory:
    """
    SQLite 持久化的 URL 去重记忆库。

    使用方法:
      mem = UrlMemory()
      if not mem.is_visited(url):
          mem.mark_visited(url, status="success")
      all_urls = mem.get_all_visited()
    """

    _instance = None
    _instance_lock = threading.Lock()

    def __new__(cls, db_path: str = None):
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    obj = super().__new__(cls)
                    obj._db_path = db_path or _DEFAULT_DB_PATH
                    obj._initialized = False
                    cls._instance = obj
        return cls._instance

    def _init_db(self):
        """延迟初始化数据库表"""
        if self._initialized:
            return
        with _MEMORY_LOCK:
            if self._initialized:
                return
            conn = sqlite3.connect(self._db_path)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS visited_urls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT UNIQUE NOT NULL,
                    base_url TEXT,
                    status TEXT DEFAULT 'pending',
                    title TEXT,
                    html_content TEXT DEFAULT NULL,
                    created_at TEXT DEFAULT (datetime('now', 'localtime'))
                )
            """)
            # ★ Phase 3: 如果旧表没有 html_content 列，尝试 ALTER TABLE 添加
            try:
                conn.execute("ALTER TABLE visited_urls ADD COLUMN html_content TEXT DEFAULT NULL")
            except sqlite3.OperationalError:
                pass  # 列已存在
            conn.execute("""
                CREATE TABLE IF NOT EXISTS agent_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT UNIQUE NOT NULL,
                    target_url TEXT,
                    state_json TEXT,
                    created_at TEXT DEFAULT (datetime('now', 'localtime')),
                    updated_at TEXT DEFAULT (datetime('now', 'localtime'))
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_url ON visited_urls(url)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_session ON agent_sessions(session_id)")
            conn.commit()
            conn.close()
            self._initialized = True
            agent_logger.info(f"UrlMemory 初始化完成 | db={self._db_path}")

    def is_visited(self, url: str) -> bool:
        """检查 URL 是否已存在于记忆库中"""
        self._init_db()
        with _MEMORY_LOCK:
            conn = sqlite3.connect(self._db_path)
            cursor = conn.execute("SELECT 1 FROM visited_urls WHERE url = ? LIMIT 1", (url,))
            result = cursor.fetchone() is not None
            conn.close()
            return result

    def get_cached_html(self, url: str) -> Optional[str]:
        """
        ★ Phase 3 新增: 获取已访问 URL 的缓存 HTML 内容。
        只有当 status='success' 且有 html_content 时才返回。
        返回 None 表示无可用缓存（需重新抓取）。
        """
        self._init_db()
        with _MEMORY_LOCK:
            conn = sqlite3.connect(self._db_path)
            cursor = conn.execute(
                "SELECT html_content, status FROM visited_urls WHERE url = ? LIMIT 1", (url,)
            )
            row = cursor.fetchone()
            conn.close()
            if row and row[1] == "success" and row[0]:
                return row[0]
            return None

    def mark_visited(self, url: str, status: str = "success", title: str = "",
                     base_url: str = "", html_content: str = ""):
        """
        将 URL 写入记忆库。
        
        ★ Phase 3 增强: html_content 参数，缓存抓取到的 HTML 内容。
        当 fetch_page 命中已访问 URL 时，可直接返回缓存的 HTML，避免重复抓取。
        """
        self._init_db()
        with _MEMORY_LOCK:
            try:
                conn = sqlite3.connect(self._db_path)
                # 如果已有记录，检查是否已有 html_content；无则写入，有则保留
                existing = conn.execute(
                    "SELECT html_content, status FROM visited_urls WHERE url = ?", (url,)
                ).fetchone()
                
                if existing:
                    old_html = existing[0]
                    old_status = existing[1]
                    # 只有当新状态是 success 且有 HTML 内容时才覆盖
                    if status == "success" and html_content:
                        conn.execute(
                            "UPDATE visited_urls SET status=?, title=?, html_content=?, created_at=datetime('now', 'localtime') WHERE url=?",
                            (status, title, html_content, url)
                        )
                    elif status == "success" and old_status != "success":
                        # 升级状态但不一定有 html（比如预检通过后重新标记）
                        conn.execute(
                            "UPDATE visited_urls SET status=?, title=? WHERE url=?",
                            (status, title, url)
                        )
                    elif old_html:
                        # 已有缓存 HTML，保留
                        pass
                    else:
                        # 更新状态和标题，html_content 有则写入
                        if html_content:
                            conn.execute(
                                "UPDATE visited_urls SET status=?, title=?, html_content=? WHERE url=?",
                                (status, title, html_content, url)
                            )
                        else:
                            conn.execute(
                                "UPDATE visited_urls SET status=?, title=? WHERE url=?",
                                (status, title, url)
                            )
                else:
                    conn.execute(
                        "INSERT OR REPLACE INTO visited_urls (url, base_url, status, title, html_content, created_at) "
                        "VALUES (?, ?, ?, ?, ?, datetime('now', 'localtime'))",
                        (url, base_url, status, title, html_content if html_content else None)
                    )
                conn.commit()
                conn.close()
            except Exception as e:
                agent_logger.warning(f"UrlMemory.mark_visited 失败: {e}")

    def mark_visited_without_blocking(self, url: str, status: str = "pre_check",
                                      title: str = "", html_content: str = ""):
        """
        ★ Phase 3 新增: 预检专用写入。
        标记 URL 但不阻止后续真正的 fetch_page 抓取。
        与 mark_visited 的区别：status 使用 'pre_check'，is_visited 仍返回 True，
        但 get_cached_html 只对 status='success' 的 URL 返回内容。
        
        注意：预检写入时如果有 HTML 内容，一并存入，以便 fetch_page 命中时直接返回。
        """
        self._init_db()
        with _MEMORY_LOCK:
            try:
                conn = sqlite3.connect(self._db_path)
                existing = conn.execute(
                    "SELECT status, html_content FROM visited_urls WHERE url = ?", (url,)
                ).fetchone()
                
                if existing:
                    # 已有预检记录且有 HTML 内容 → 不覆盖
                    # 如果是旧预检记录，升级为有内容的记录
                    old_status = existing[0]
                    old_html = existing[1]
                    if old_status != "success" and html_content:
                        conn.execute(
                            "UPDATE visited_urls SET status=?, title=?, html_content=?, created_at=datetime('now', 'localtime') WHERE url=?",
                            (status, title, html_content, url)
                        )
                else:
                    conn.execute(
                        "INSERT OR REPLACE INTO visited_urls (url, base_url, status, title, html_content, created_at) "
                        "VALUES (?, ?, ?, ?, ?, datetime('now', 'localtime'))",
                        (url, "", status, title, html_content if html_content else None)
                    )
                conn.commit()
                conn.close()
            except Exception as e:
                agent_logger.warning(f"UrlMemory.mark_visited_without_blocking 失败: {e}")

    def update_status(self, url: str, status: str, title: str = ""):
        """更新 URL 抓取状态"""
        self._init_db()
        with _MEMORY_LOCK:
            try:
                conn = sqlite3.connect(self._db_path)
                if title:
                    conn.execute(
                        "UPDATE visited_urls SET status=?, title=? WHERE url=?",
                        (status, title, url)
                    )
                else:
                    conn.execute(
                        "UPDATE visited_urls SET status=? WHERE url=?",
                        (status, url)
                    )
                conn.commit()
                conn.close()
            except Exception as e:
                agent_logger.warning(f"UrlMemory.update_status 失败: {e}")

    def get_all_visited(self, base_url_filter: str = "") -> List[str]:
        """获取所有已访问 URL（可选按 base_url 过滤）"""
        self._init_db()
        with _MEMORY_LOCK:
            conn = sqlite3.connect(self._db_path)
            if base_url_filter:
                cursor = conn.execute(
                    "SELECT url FROM visited_urls WHERE base_url = ?", (base_url_filter,)
                )
            else:
                cursor = conn.execute("SELECT url FROM visited_urls")
            urls = [row[0] for row in cursor.fetchall()]
            conn.close()
            return urls

    def count_visited(self, base_url_filter: str = "") -> int:
        """统计已访问 URL 数量"""
        self._init_db()
        with _MEMORY_LOCK:
            conn = sqlite3.connect(self._db_path)
            if base_url_filter:
                cursor = conn.execute(
                    "SELECT COUNT(*) FROM visited_urls WHERE base_url = ?", (base_url_filter,)
                )
            else:
                cursor = conn.execute("SELECT COUNT(*) FROM visited_urls")
            result = cursor.fetchone()[0]
            conn.close()
            return result

    def save_session(self, session_id: str, target_url: str, state_json: str):
        """保存 Agent 会话状态快照"""
        self._init_db()
        with _MEMORY_LOCK:
            try:
                conn = sqlite3.connect(self._db_path)
                conn.execute(
                    "INSERT OR REPLACE INTO agent_sessions (session_id, target_url, state_json, updated_at) "
                    "VALUES (?, ?, ?, datetime('now', 'localtime'))",
                    (session_id, target_url, state_json)
                )
                conn.commit()
                conn.close()
            except Exception as e:
                agent_logger.warning(f"UrlMemory.save_session 失败: {e}")

    def load_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """加载 Agent 会话状态"""
        self._init_db()
        with _MEMORY_LOCK:
            conn = sqlite3.connect(self._db_path)
            cursor = conn.execute(
                "SELECT state_json FROM agent_sessions WHERE session_id = ? LIMIT 1",
                (session_id,)
            )
            row = cursor.fetchone()
            conn.close()
            if row:
                try:
                    return json.loads(row[0])
                except json.JSONDecodeError:
                    return None
            return None

    def clear_session(self, session_id: str):
        """清除指定会话"""
        self._init_db()
        with _MEMORY_LOCK:
            conn = sqlite3.connect(self._db_path)
            conn.execute("DELETE FROM agent_sessions WHERE session_id = ?", (session_id,))
            conn.commit()
            conn.close()

    def close(self):
        """关闭数据库连接（SQLite 自动管理，此方法预留）"""
        pass

    # ==================================================================
    # ★ 重置入口：清空脏历史，实现"白纸启动"
    # ==================================================================

    def clear_site(self, base_url: str) -> int:
        """
        清除指定站点的所有 visited_urls 记录（包括缓存 HTML）。
        新 run 不再读取到旧 run 的短 HTML / 脏状态。

        Returns: 删除的记录数
        """
        self._init_db()
        with _MEMORY_LOCK:
            conn = sqlite3.connect(self._db_path)
            cursor = conn.execute(
                "DELETE FROM visited_urls WHERE base_url = ?", (base_url,)
            )
            deleted = cursor.rowcount
            conn.commit()
            conn.close()
            agent_logger.info(f"[UrlMemory] 已清除站点 {base_url} 的 {deleted} 条记录")
            return deleted

    def clear_all(self) -> int:
        """
        清除所有 visited_urls 记录（全量重置）。

        Returns: 删除的记录数
        """
        self._init_db()
        with _MEMORY_LOCK:
            conn = sqlite3.connect(self._db_path)
            cursor = conn.execute("DELETE FROM visited_urls")
            deleted = cursor.rowcount
            conn.commit()
            conn.close()
            agent_logger.info(f"[UrlMemory] 已清除全部 {deleted} 条 visited_urls 记录")
            return deleted