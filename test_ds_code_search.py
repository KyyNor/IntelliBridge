#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
DS 代码检索工具测试脚本

用法: 修改下面的测试参数，然后运行:
    python test_ds_code_search.py
"""

import json
import sys
sys.path.insert(0, '.')

# 导入待测试的工具
from tools.ds_code_search import datafactory_sql_search, datafactory_task_info, ds_code_search


# =============================================================================
# 测试参数配置区
# =============================================================================

# 选择要运行的测试类型
TEST_TYPE = "task_info"  # 可选值: "sql_search" 或 "task_info"


# ---------- datafactory_task_info 测试参数 ----------
TASK_INFO_PARAMS = {
    # 以下参数至少填写一个，推荐根据测试场景选择
    # "code_path": "对公",      # 路径模糊匹配（忽略大小写），支持部分路径
    # "code_path": "/ds/ODS/对公有效户/",  # 也可以填完整路径，前缀/包含都支持
    "code_path": None,

    # "from_table": "dws_acct_abs_add_bal_1000_gr",   # 输入表模糊匹配（忽略大小写），不区分库名
    "from_table": None,

    # "to_table": None,                # 输出表模糊匹配（忽略大小写），不区分库名
    "to_table": "dws_acct_abs_add_bal_1000_gr",
}


# ---------- datafactory_sql_search 测试参数 ----------
SQL_SEARCH_PARAMS = {
    # "pattern": r"from\s+\w+",        # 正则表达式（忽略大小写），匹配 SQL 中的 FROM 子句
    # "pattern": "INSERT\\s+INTO",   # 匹配 INSERT INTO
    # "pattern": "JOIN\\s+\\w+",     # 匹配 JOIN
    "pattern": "as absorb_xc_info",           # 匹配包含 window 的行

    "code_path": "对公有效户",             # 路径过滤，支持前缀/包含/模糊匹配（忽略大小写）
    # "code_path": "/ds/ODS/",       # 精确前缀匹配
    # "code_path": "对公",            # 模糊匹配：路径中任意位置包含"对公"

    "before": 10,                     # 匹配行往前显示的行数（显示上下文）
    "after": 10,                      # 匹配行往后显示的行数（显示上下文）

    "code_status": "已上线",          # 状态过滤："已上线"、"未上线"
    # "code_status": "未上线",

    "page": 1,                        # 页码
    "page_size": 1000,                 # 每页条数（最大1000）
}


# =============================================================================
# 测试执行代码（一般无需修改）
# =============================================================================

def test_task_info():
    """测试 datafactory_task_info"""
    params = TASK_INFO_PARAMS.copy()

    print("\n" + "="*60)
    print("测试 datafactory_task_info")
    print("="*60)
    print(f"输入参数: {params}")

    # 确保缓存已加载（如果没有的话触发加载）
    ds_code_search.ensure_cache()

    result_str = datafactory_task_info(**params)
    result = json.loads(result_str)

    if result and isinstance(result, list) and "error" in result[0]:
        print(f"\n❌ 错误: {result[0]['error']}")
        return

    print(f"\n✅ 找到 {len(result)} 条任务")
    print("\n--- 前3条任务预览 ---")
    for i, task in enumerate(result[:3]):
        print(f"\n[{i+1}] {task.get('code_path', 'N/A')}")
        print(f"    任务状态: {task.get('任务状态', 'N/A')}")
        print(f"    任务类型: {task.get('任务类型', 'N/A')}")
        print(f"    代码行数: {task.get('代码行数', 'N/A')}")
        print(f"    输入表: {task.get('输入表清单', [])}")
        print(f"    输出表: {task.get('输出表清单', [])}")


def test_sql_search():
    """测试 datafactory_sql_search"""
    params = SQL_SEARCH_PARAMS.copy()

    print("\n" + "="*60)
    print("测试 datafactory_sql_search")
    print("="*60)
    print(f"输入参数: {params}")

    # 确保缓存已加载
    ds_code_search.ensure_cache()

    result_str = datafactory_sql_search(**params)
    result = json.loads(result_str)

    if "error" in result:
        print(f"\n❌ 错误: {result['error']}")
        return

    matches = result.get("matches", [])
    pagination = result.get("pagination", {})

    print(f"\n✅ 共找到 {pagination.get('total', 0)} 个任务（当前页 {len(matches)} 条）")
    print(f"分页信息: 第{pagination.get('page')}/{pagination.get('total_pages')}页，每页{pagination.get('page_size')}条")

    # 打印所有匹配结果
    for i, match in enumerate(matches):
        print(f"\n{'='*60}")
        print(f"[{i+1}/{len(matches)}] {match.get('code_path', 'N/A')}")
        print(f"{'='*60}")
        print(f"    匹配行数: {match.get('匹配行数')}")
        print(f"    行号: {match.get('行号')}")
        print(f"    任务状态: {match.get('任务状态')}")
        print(f"    输入表: {match.get('from_database_table', [])}")
        print(f"    输出表: {match.get('to_database_table', [])}")

        # 显示所有匹配行的上下文
        all_contexts = match.get("所有匹配行上下文", [])
        print(f"\n    --- 所有匹配行的上下文 ({len(all_contexts)}个) ---")
        for ctx in all_contexts:
            print(f"\n    ▶ 行号 {ctx.get('行号')}:")
            print("    " + "-"*40)
            snippet = ctx.get('代码片段', '')
            # 每行加上缩进，方便阅读
            for line in snippet.split('\n'):
                print(f"    {line}")

        print(f"\n{'='*60}\n")


if __name__ == "__main__":
    if TEST_TYPE == "task_info":
        test_task_info()
    elif TEST_TYPE == "sql_search":
        test_sql_search()
    else:
        print(f"❌ 未知的测试类型: {TEST_TYPE}")
        print("请设置 TEST_TYPE = 'sql_search' 或 'task_info'")