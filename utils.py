"""
工具模块：MySQL数据库操作与本地文件备份
"""

import os
import re
import pymysql
from datetime import datetime
from typing import Dict, Any, Optional
import config


# ==================== 数据库操作 ====================

class ReportDB:
    """MySQL 数据库操作类，管理 scraped_pages 表"""

    def __init__(self):
        self.conn: Optional[pymysql.Connection] = None
        self.cursor = None

    def connect(self) -> bool:
        """建立数据库连接"""
        try:
            self.conn = pymysql.connect(
                host=config.DB_HOST,
                port=config.DB_PORT,
                user=config.DB_USER,
                password=config.DB_PASSWORD,
                database=config.DB_NAME,
                charset="utf8mb4",
                cursorclass=pymysql.cursors.DictCursor,
            )
            self.cursor = self.conn.cursor()
            print(f"  [DB] 已连接 {config.DB_HOST}:{config.DB_PORT}/{config.DB_NAME}")
            return True
        except pymysql.err.OperationalError as e:
            print(f"  [DB错误] 连接失败: {e}")
            return False
        except Exception as e:
            print(f"  [DB错误] 未知错误: {e}")
            return False

    def close(self):
        """关闭数据库连接"""
        if self.cursor:
            self.cursor.close()
        if self.conn:
            self.conn.close()
        print("  [DB] 连接已关闭")

    def ensure_table(self) -> bool:
        """确保 scraped_pages 表存在，不存在则自动创建"""
        if not self.conn or not self.cursor:
            print("  [DB错误] 未建立连接，请先调用 connect()")
            return False

        sql = f"""
        CREATE TABLE IF NOT EXISTS {config.DB_TABLE} (
            id INT AUTO_INCREMENT PRIMARY KEY,
            gsmc VARCHAR(255) NOT NULL DEFAULT '' COMMENT '公司名称',
            ywlx1 VARCHAR(100) DEFAULT '无' COMMENT '一级栏目',
            ywlx2 VARCHAR(100) DEFAULT '无' COMMENT '二级栏目',
            ywlx3 VARCHAR(100) DEFAULT '无' COMMENT '三级栏目',
            ywlx4 VARCHAR(100) DEFAULT '无' COMMENT '四级栏目',
            riqi DATETIME DEFAULT NULL COMMENT '发布时间',
            title VARCHAR(500) DEFAULT '' COMMENT '页面标题',
            url VARCHAR(500) NOT NULL DEFAULT '' COMMENT '网页URL',
            html_content MEDIUMTEXT COMMENT '清洗后HTML',
            summary TEXT COMMENT 'AI摘要',
            gsjj TEXT COMMENT '公司简介',
            ywfw TEXT COMMENT '业务范围/主营业务',
            lxdh VARCHAR(100) DEFAULT '无' COMMENT '联系电话',
            dzyx VARCHAR(100) DEFAULT '无' COMMENT '电子邮箱',
            gsdz VARCHAR(300) DEFAULT '无' COMMENT '公司地址',
            czhm VARCHAR(100) DEFAULT '无' COMMENT '传真号码',
            yzbm VARCHAR(20) DEFAULT '无' COMMENT '邮政编码',
            create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
            UNIQUE KEY uk_url (url(255)),
            INDEX idx_gsmc (gsmc),
            INDEX idx_ywlx1 (ywlx1),
            INDEX idx_create_time (create_time)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """
        try:
            self.cursor.execute(sql)
            self.conn.commit()
            print(f"  [DB] 表 {config.DB_TABLE} 就绪")
            return True
        except Exception as e:
            print(f"  [DB错误] 建表失败: {e}")
            return False

    def insert_page(self, data: Dict[str, Any]) -> bool:
        """
        插入一条页面记录，URL去重：已存在则跳过
        返回 True=成功插入, False=已存在跳过
        """
        if not self.conn or not self.cursor:
            print("  [DB错误] 未建立连接")
            return False

        url = data.get("url", "")
        if not url:
            return False

        # 先检查 URL 是否已存在
        check_sql = f"SELECT id FROM {config.DB_TABLE} WHERE url = %s LIMIT 1"
        try:
            self.cursor.execute(check_sql, (url,))
            if self.cursor.fetchone():
                return False  # 已存在，跳过
        except Exception:
            pass

        # 解析日期
        riqi_str = data.get("riqi", "")
        riqi = None
        if riqi_str and riqi_str != "无":
            try:
                formats = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"]
                for fmt in formats:
                    try:
                        riqi = datetime.strptime(riqi_str, fmt)
                        break
                    except ValueError:
                        continue
            except Exception:
                pass

        sql = f"""
        INSERT INTO {config.DB_TABLE}
            (gsmc, ywlx1, ywlx2, ywlx3, ywlx4, riqi, title, url,
             html_content, summary, gsjj, ywfw, lxdh, dzyx, gsdz, czhm, yzbm)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        try:
            self.cursor.execute(sql, (
                data.get("gsmc", ""),
                data.get("ywlx1", "无"),
                data.get("ywlx2", "无"),
                data.get("ywlx3", "无"),
                data.get("ywlx4", "无"),
                riqi,
                data.get("title", ""),
                url,
                data.get("html", ""),
                data.get("summary", ""),
                data.get("gsjj", "无"),
                data.get("ywfw", "无"),
                data.get("lxdh", "无"),
                data.get("dzyx", "无"),
                data.get("gsdz", "无"),
                data.get("czhm", "无"),
                data.get("yzbm", "无"),
            ))
            self.conn.commit()
            return True
        except pymysql.err.IntegrityError:
            # URL重复（并发场景下的二次保护）
            return False
        except Exception as e:
            print(f"  [DB错误] 写入失败 [{url[:60]}]: {e}")
            self.conn.rollback()
            raise


# ==================== 本地文件备份 ====================

def save_local_backup(data: Dict[str, Any]) -> Optional[str]:
    """
    保存HTML文件到本地，按「公司名/一级栏目/二级栏目/」层级目录存放
    文件名取页面标题，非法字符替换为下划线
    """
    gsmc = data.get("gsmc", "未识别")
    ywlx1 = data.get("ywlx1", "无")
    ywlx2 = data.get("ywlx2", "无")
    title = data.get("title", "untitled")
    html = data.get("html", "")

    if not html:
        return None

    dir_parts = [config.LOCAL_BACKUP_DIR]
    for part in [gsmc, ywlx1, ywlx2]:
        if part and part != "无":
            safe = _safe_filename(part)
            if safe:
                dir_parts.append(safe)

    backup_dir = os.path.join(*dir_parts)
    try:
        os.makedirs(backup_dir, exist_ok=True)
    except Exception as e:
        print(f"  [备份] 创建目录失败 {backup_dir}: {e}")
        return None

    filename = _safe_filename(title)
    if not filename:
        filename = "untitled"
    filepath = os.path.join(backup_dir, f"{filename}.html")
    counter = 1
    while os.path.exists(filepath):
        filepath = os.path.join(backup_dir, f"{filename}_{counter}.html")
        counter += 1

    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(html)
        return filepath
    except Exception as e:
        print(f"  [备份] 写入文件失败 {filepath}: {e}")
        return None


def _safe_filename(name: str) -> str:
    """将字符串转为安全的文件名，去除或替换非法字符"""
    if not name:
        return ""
    name = re.sub(r'[\\/:*?"<>|]', '_', name)
    name = name.strip().strip(".")
    if len(name) > 80:
        name = name[:80]
    return name
