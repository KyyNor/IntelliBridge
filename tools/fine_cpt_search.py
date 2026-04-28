"""帆软 CPT 血缘搜索工具 """

import regex
import json
import threading
from typing import Optional, List, Dict, Any
from datetime import datetime

from utils.mysql_pool import mysql_pool
from utils.logger import logger
from utils.decorators import log_function_info
from fastmcp import FastMCP

fine_cpt_mcp = FastMCP("IntelliBridge FineCPT Search")


class LineageInfo:
    """单条血缘信息"""

    def __init__(
        self,
        full_table_name: str,
        db_name: Optional[str] = None,
        table_name: Optional[str] = None,
        db_type: Optional[str] = None,
        host: Optional[str] = None,
        port: Optional[str] = None,
        link: Optional[str] = None,
        lineage_type: Optional[str] = None,
    ):
        self.full_table_name = full_table_name or ""
        self.db_name = db_name
        self.table_name = table_name
        self.db_type = db_type
        self.host = host
        self.port = port
        self.link = link  # 用于跳转链接（target_link）
        self.lineage_type = lineage_type  # "0"/"1"/"2"/"3"


class FineCptObject:
    """单个 CPT 文件的聚合血缘对象"""

    def __init__(self, pk: str):
        # 唯一键：用 cpt_file_path 而非 full_template_path（前者更稳定）
        self.pk = pk
        self.display_name: Optional[str] = None
        self.full_template_path: Optional[str] = None
        self.mount_type: Optional[str] = None
        self.cpt_file_path: Optional[str] = None
        self.base_sub_dir: Optional[str] = None
        # 聚合的血缘数据
        self.source_tables: List[LineageInfo] = []
        self.target_tables: List[LineageInfo] = []
        self.jump_links: List[LineageInfo] = []
        self.api_calls: List[LineageInfo] = []  # 待扩展
        # 保留原始行信息（去重用）
        self._raw_rows: List[Dict] = []

    def add_row(self, row: Dict):
        self._raw_rows.append(row)
        lt = str(row.get("lineage_type", ""))

        info = LineageInfo(
            full_table_name=row.get("full_table_name") or "",
            db_name=row.get("db_name"),
            table_name=row.get("table_name"),
            db_type=row.get("db_type"),
            host=row.get("host"),
            port=row.get("port"),
            link=row.get("target_link"),
            lineage_type=lt,
        )

        if lt == "0":
            self.source_tables.append(info)
        elif lt == "1":
            self.target_tables.append(info)
        elif lt == "2":
            self.jump_links.append(info)
        elif lt == "3":
            self.api_calls.append(info)

    def seal(self):
        """填充共同字段（基于第一条出现的值）"""
        if not self._raw_rows:
            return
        r = self._raw_rows[0]
        self.display_name = r.get("display_name")
        self.full_template_path = r.get("full_template_path")
        self.mount_type = r.get("mount_type")
        self.cpt_file_path = r.get("cpt_file_path")
        self.base_sub_dir = r.get("base_sub_dir")

    def to_dict(self) -> Dict[str, Any]:
        def infos_to_list(infos: List[LineageInfo]) -> List[Dict]:
            return [
                {
                    "full_table_name": i.full_table_name,
                    "db_name": i.db_name,
                    "table_name": i.table_name,
                    "db_type": i.db_type,
                    "host": i.host,
                    "port": i.port,
                    "link": i.link,
                }
                for i in infos
                if i.full_table_name or i.link
            ]

        return {
            "pk": self.pk,
            "display_name": self.display_name,
            "full_template_path": self.full_template_path,
            "mount_type": self.mount_type,
            "cpt_file_path": self.cpt_file_path,
            "base_sub_dir": self.base_sub_dir,
            "source_tables": infos_to_list(self.source_tables),
            "target_tables": infos_to_list(self.target_tables),
            "jump_links": infos_to_list(self.jump_links),
        }


class FineCptSearch:
    """帆软 CPT 血缘搜索工具类"""

    MAX_LIMIT = 1000
    DEFAULT_PAGE_SIZE = 20
    REGEX_TIMEOUT = 5  # 秒
    CACHE_REFRESH_INTERVAL = 8 * 60 * 60  # 8小时

    def __init__(self):
        # 主缓存：pk -> FineCptObject
        self._cache: Dict[str, FineCptObject] = {}

        # 辅助索引
        self._index_by_db_table: Dict[str, List[str]] = {}   # 完整表名 -> pk列表（来源/去向通用）
        self._index_by_project: Dict[str, List[str]] = {}   # base_sub_dir -> pk列表
        self._index_by_display_name: Dict[str, List[str]] = {}  # 显示名 -> pk列表（模糊）

        # 缓存状态
        self._cache_loaded = False
        self._last_load_time: Optional[datetime] = None
        self._record_count = 0  # 原始记录条数

        # 定时刷新
        self._refresh_timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()

    # ========== 缓存加载 ==========

    def load_cache(self, force: bool = False) -> bool:
        """
        加载血缘数据到内存缓存

        数据来源: data_factory.metadata_fine_cpt_lineage
        表结构特点：一行 = CPT文件 × 单一血缘关系，需在内存中按 pk 聚合
        """
        with self._lock:
            if self._cache_loaded and not force:
                logger.info("FineCpt 缓存已加载，跳过")
                return True

            logger.info("开始加载 FineCpt 血缘数据到缓存...")
            try:
                sql = """
                    SELECT
                        id,
                        display_name,
                        full_template_path,
                        mount_type,
                        cpt_file_path,
                        base_sub_dir,
                        db_name,
                        table_name,
                        full_table_name,
                        host,
                        port,
                        db_type,
                        lineage_type,
                        target_link
                    FROM metadata_fine_cpt_lineage
                """

                with mysql_pool.get_connection("mysql_121_data_factory") as conn:
                    cursor = conn.cursor()
                    cursor.execute(sql, [])
                    rows = cursor.fetchall()
                    cursor.close()

                self._cache.clear()
                self._index_by_db_table.clear()
                self._index_by_project.clear()
                self._index_by_display_name.clear()

                # 第一步：按 PK 聚合（pk = cpt_file_path，同一文件多条血缘记录）
                grouped: Dict[str, FineCptObject] = {}

                for row in rows:
                    pk = row.get("cpt_file_path") or row.get("full_template_path") or str(row.get("id"))
                    if pk not in grouped:
                        grouped[pk] = FineCptObject(pk)
                    grouped[pk].add_row(row)

                # 第二步：seal 每个对象并建索引
                for pk, obj in grouped.items():
                    obj.seal()
                    self._cache[pk] = obj

                    # 建表索引（来源表 + 目标表 均收入）
                    for st in obj.source_tables:
                        tbl = st.full_table_name
                        if tbl:
                            self._add_to_index(self._index_by_db_table, tbl, pk)
                    for tt in obj.target_tables:
                        tbl = tt.full_table_name
                        if tbl:
                            self._add_to_index(self._index_by_db_table, tbl, pk)

                    # 建项目索引
                    proj = obj.base_sub_dir
                    if proj:
                        self._add_to_index(self._index_by_project, proj, pk)

                    # 建显示名索引（精准 + 模糊）
                    dn = obj.display_name
                    if dn:
                        self._add_to_index(self._index_by_display_name, dn, pk)
                        # 同时对每个词做索引（支持空格分隔的多词）
                        for token in dn.replace("/", " ").replace("\\", " ").split():
                            if token:
                                self._add_to_index(self._index_by_display_name, token, pk)

                self._record_count = len(rows)
                self._cache_loaded = True
                self._last_load_time = datetime.now()
                logger.info(
                    f"FineCpt 缓存加载完成：{len(self._cache)} 个 CPT 文件，{self._record_count} 条血缘记录"
                )

                # 调度下次刷新
                self._schedule_refresh()
                return True

            except Exception as e:
                logger.error(f"FineCpt 缓存加载失败: {e}")
                return False

    def _add_to_index(self, index: Dict[str, List[str]], key: str, pk: str):
        key_lower = key.lower()
        if key_lower not in index:
            index[key_lower] = []
        if pk not in index[key_lower]:
            index[key_lower].append(pk)

    def _schedule_refresh(self):
        if self._refresh_timer:
            self._refresh_timer.cancel()
        self._refresh_timer = threading.Timer(
            self.CACHE_REFRESH_INTERVAL,
            self.load_cache,
            kwargs={"force": True},
        )
        self._refresh_timer.daemon = True
        self._refresh_timer.start()
        logger.info(f"已调度 FineCpt 缓存刷新，间隔 {self.CACHE_REFRESH_INTERVAL} 秒")

    def ensure_cache(self):
        """懒加载确保缓存已就绪"""
        if not self._cache_loaded:
            self.load_cache()

    def get_cache_stats(self) -> Dict[str, Any]:
        """获取缓存统计"""
        return {
            "loaded": self._cache_loaded,
            "cpt_file_count": len(self._cache),
            "raw_record_count": self._record_count,
            "last_load_time": self._last_load_time.isoformat() if self._last_load_time else None,
            "index_by_db_table_keys": len(self._index_by_db_table),
            "index_by_project_keys": len(self._index_by_project),
            "index_by_display_name_keys": len(self._index_by_display_name),
        }

    # ========== 路径/关键词匹配 ==========

    def _match_pk(self, pk: str, filter_text: str) -> bool:
        """
        判断 PK（cpt_file_path）是否匹配过滤条件

        支持：
        - 前缀匹配："/data_center/xxx/"
        - 包含匹配（忽略大小写）：任意中间路径段
        """
        if not filter_text:
            return True
        ft = filter_text.strip().rstrip("/")
        if not ft or ft in ("/fine", "/cpt"):
            return True
        # 忽略大小写的包含匹配
        if ft.lower() in pk.lower():
            return True
        return False

    def _match_table(self, table_name: str, filter_text: str) -> bool:
        """判断表名是否匹配过滤条件（忽略大小写，前缀/包含混合）"""
        if not filter_text:
            return True
        ft = filter_text.strip()
        if not ft:
            return True
        t_lower = table_name.lower()
        ft_lower = ft.lower()
        # 精确匹配
        if t_lower == ft_lower:
            return True
        # 后缀匹配（输入 dim_ 则匹配所有 dim_ 开头的表）
        if t_lower.startswith(ft_lower.rstrip("*")):
            return True
        # 包含匹配
        if ft_lower in t_lower:
            return True
        return False

    # ========== 血缘检索（核心方法）==========

    def search_lineage(
        self,
        code_path: Optional[str] = None,
        source_table: Optional[str] = None,
        target_table: Optional[str] = None,
        jump_link: Optional[str] = None,     # 跳转链接关键词
        lineage_type: Optional[str] = None,   # "0"/"1"/"2"/"3" 或 "全部"
        display_name: Optional[str] = None,  # 显示名关键词
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        """
        血缘信息检索

        核心查找方向：
        - 按 path/display_name 找 CPT -> 返回其全量血缘
        - 按 source_table/target_table 找上游/下游表 -> 反向聚合出所有使用的 CPT
        - 多条件交叉过滤
        """
        # 限制页大小
        page_size = min(max(page_size, 1), 50)

        self.ensure_cache()
        logger.info(
            f"FineCpt 血缘检索: code_path={code_path}, source_table={source_table}, "
            f"target_table={target_table}, lineage_type={lineage_type}"
        )

        # ---------- 第一步：收集候选 PK ----------
        candidate_pks: Optional[set] = None  # None 表示不限
        do_cross_filter = False  # 是否做交集（多条件AND）

        # 1.1 按路径（cpt_file_path）过滤
        if code_path:
            pks = {
                pk for pk, obj in self._cache.items()
                if self._match_pk(pk, code_path) or self._match_pk(obj.full_template_path or "", code_path)
            }
            candidate_pks = candidate_pks & pks if candidate_pks else pks

        # 1.2 按显示名过滤
        if display_name:
            pks = set()
            dl = display_name.strip().lower()
            for key, pk_list in self._index_by_display_name.items():
                if dl in key:
                    pks.update(pk_list)
            candidate_pks = candidate_pks & pks if candidate_pks else pks

        # 1.3 按来源表过滤 -> 需要跨索引，且结果应当和前面条件AND
        if source_table:
            pks = self._lookup_table_index(source_table, self._index_by_db_table, lambda obj: obj.source_tables)
            if candidate_pks:
                candidate_pks &= pks
            else:
                candidate_pks = pks
            do_cross_filter = True

        # 1.4 按目标表过滤
        if target_table:
            pks = self._lookup_table_index(target_table, self._index_by_db_table, lambda obj: obj.target_tables)
            if candidate_pks:
                candidate_pks &= pks
            else:
                candidate_pks = pks
            do_cross_filter = True

        # 1.5 如果没有任何过滤条件，默认全集
        if not do_cross_filter and not any([code_path, display_name]):
            candidate_pks = None  # 不限制

        # ---------- 第二步：枚举候选，逐一匹配 ----------
        all_results: List[FineCptObject] = []

        for pk, obj in self._cache.items():
            # 无候选集时跳过已在前面过滤掉的
            if candidate_pks is not None and pk not in candidate_pks:
                continue

            # 血统类型精细过滤（若有来源/目标表过滤则不需要再过滤 lineage_type，
            # 此参数主要用于在没有表过滤时单独按类型筛）
            if lineage_type and lineage_type != "全部":
                hits = False
                if lineage_type == "0":
                    hits = bool(obj.source_tables)
                elif lineage_type == "1":
                    hits = bool(obj.target_tables)
                elif lineage_type == "2":
                    hits = bool(obj.jump_links)
                if not hits:
                    continue

            # 跳转链接关键词过滤（针对 lineage_type==2 的额外过滤）
            if jump_link:
                jl_match = any(
                    self._match_table(jinfo.link or "", jump_link) or self._match_table(jinfo.full_table_name, jump_link)
                    for jinfo in obj.jump_links
                )
                if not jl_match:
                    continue

            all_results.append(obj)

        # ---------- 第三步：去重（pk 已经唯一，此处无需去重）----------

        # ---------- 第四步：分页 ----------
        total = len(all_results)
        total_pages = (total + page_size - 1) // page_size if page_size > 0 else 0
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        page_objs = all_results[start_idx:end_idx]

        results = [obj.to_dict() for obj in page_objs]

        logger.info(f"FineCpt 血缘检索完成，共 {total} 个 CPT 文件符合条件")
        return {
            "results": results,
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total": total,
                "total_pages": total_pages,
            },
        }

    def _lookup_table_index(
        self,
        filter_text: str,
        index: Dict[str, List[str]],
        attr_accessor,
    ) -> set:
        """在索引中查找匹配 filter_text 的所有 pk，利用二级索引加速"""
        ft = filter_text.strip().lower()
        if not ft:
            return set()

        result_pks = set()

        # 精准命中（一级索引查到则直接返回）
        if ft in index:
            result_pks.update(index[ft])

        # 通配前缀（前缀 * 结尾 => 做 startswith 扫描）
        # 如输入 "dim_" => 找所有 dim_* 开头的表
        if ft.endswith("_") or ft.endswith("."):
            prefix = ft.rstrip("_").rstrip(".")
            for key, pk_list in index.items():
                if key.startswith(prefix):
                    result_pks.update(pk_list)

        # 包含匹配（二次扫描，idx 可能稀疏故做补救）
        for key, pk_list in index.items():
            if ft in key and key not in index:
                result_pks.update(pk_list)

        return result_pks

    # ========== 表级别查询（哪些 CPT 用了某表 / 被某表写入）==========

    def query_cpts_by_table(
        self,
        table_name: str,
        direction: str = "all",  # "source" / "target" / "all"
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        """
        根据表名查询所有关联的 CPT 文件

        Args:
            table_name: 表名（支持模糊匹配，自动加通配）
            direction: 查来源表/source，还是目标表/target，或 all
            page/page_size: 分页
        """
        page_size = min(max(page_size, 1), 50)
        self.ensure_cache()

        candidates: Optional[set] = None

        if direction in ("source", "all"):
            src_pks = self._lookup_table_index(
                table_name, self._index_by_db_table,
                lambda obj: obj.source_tables
            )
            candidates = src_pks if direction == "source" else candidates.union(src_pks) if candidates else src_pks

        if direction in ("target", "all"):
            tgt_pks = self._lookup_table_index(
                table_name, self._index_by_db_table,
                lambda obj: obj.target_tables
            )
            if candidates is None:
                candidates = tgt_pks
            else:
                candidates |= tgt_pks

        if not candidates:
            return {
                "results": [],
                "pagination": {"page": page, "page_size": page_size, "total": 0, "total_pages": 0},
            }

        all_pks = sorted(candidates)
        total = len(all_pks)
        total_pages = (total + page_size - 1) // page_size if page_size > 0 else 0
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        page_pks = all_pks[start_idx:end_idx]

        results = [self._cache[pk].to_dict() for pk in page_pks if pk in self._cache]

        return {
            "results": results,
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total": total,
                "total_pages": total_pages,
            },
        }


# =============================================================================
# 默认实例
# =============================================================================

fine_cpt_search = FineCptSearch()


# =============================================================================
# MCP 工具
# =============================================================================

@fine_cpt_mcp.tool(name="lineage")
@log_function_info
def cpt_lineage_search(
    code_path: Optional[str] = None,
    source_table: Optional[str] = None,
    target_table: Optional[str] = None,
    jump_link: Optional[str] = None,
    lineage_type: Optional[str] = None,
    display_name: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
) -> str:
    """
    帆软 CPT 血缘检索工具

    在已采集的 CPT 血缘数据中进行多维度检索，返回满足条件的 CPT 文件及其完整血缘信息。

    典型应用场景：
    - 想看某个 CPT 的所有上游表（数据来源）→ code_path 填报表路径，lineage_type=0
    - 想看某个 CPT 的所有下游表（填报去向）→ code_path 填报表路径，lineage_type=1
    - 想知道哪些 CPT 用到了某张表（上游追溯）→ source_table 填表名
    - 想知道哪些 CPT 向某张表写了数据（下游影响面）→ target_table 填表名
    - 想看所有跳转链接关系 → lineage_type=2
    - 只想找某项目的报表 → code_path 填目录前缀
    - 想找名字包含某些字的报表 → display_name 填关键词

    Args:
        code_path: CPT 文件路径过滤（前缀/包含匹配），如 "data_center/daifa/"
        source_table: 上游表名过滤（支持模糊匹配、前缀*，如 "dim_" 匹配所有 dim_ 开头的表）
        target_table: 下游表名过滤（同上）
        jump_link: 跳转链接关键词（匹配 target_link 或完整表名字段）
        lineage_type: 血缘类型，"0"=来源表，"1"=去向表，"2"=跳转链接，"3"=API调用（暂不可用），"全部"（默认）
        display_name: 报表显示名关键词（忽略大小写，含空格分词）
        page: 页码，从1开始（可选，默认1）
        page_size: 每页返回的 CPT 文件数，默认20，最多50（可选）

    Returns:
        JSON 格式的检索结果，含 results（CPT 对象列表）及 pagination（分页信息）
    """
    result = fine_cpt_search.search_lineage(
        code_path=code_path,
        source_table=source_table,
        target_table=target_table,
        jump_link=jump_link,
        lineage_type=lineage_type,
        display_name=display_name,
        page=page,
        page_size=page_size,
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


@fine_cpt_mcp.tool(name="by_table")
@log_function_info
def cpt_search_by_table(
    table_name: str,
    direction: str = "all",
    page: int = 1,
    page_size: int = 20,
) -> str:
    """
    根据表名查找所有关联的 CPT 文件

    这是最常用的逆向查询入口：通过一张表名，找到所有用到它作为数据来源或写入目标的 CPT 报表。

    与 lineage_search 的区别在于：本接口专门服务于"以表为中心"的反查，是血缘追溯的核心工具；
    lineage_search 则更适合以 CPT 为中心的正查或多条件交叉过滤。

    Args:
        table_name: 要查询的表名（必填，支持模糊匹配，如 "dim_" 等）
        direction: 查询方向，"source"=只看将此表作为数据来源的 CPT，"target"=只看向此表写入的 CPT，
                   "all"（默认）=两者均返回
        page: 页码，从1开始（可选，默认1）
        page_size: 每页返回的 CPT 数，默认20，最多50（可选）

    Returns:
        JSON 格式的检索结果，含 results 及 pagination
    """
    if not table_name:
        return json.dumps({"error": "table_name 参数不能为空"}, ensure_ascii=False, indent=2)
    result = fine_cpt_search.query_cpts_by_table(
        table_name=table_name,
        direction=direction,
        page=page,
        page_size=page_size,
    )
    return json.dumps(result, ensure_ascii=False, indent=2)

