from diskcache import Cache
from pathlib import Path
from typing import Any, Optional


class CacheManager:
    """缓存工具类，基于 diskcache"""

    def __init__(self, cache_dir: str = None):
        """
        初始化缓存管理器

        Args:
            cache_dir: 缓存文件存储目录，默认为项目根目录下的 cache 文件夹
        """
        if cache_dir is None:
            # 获取项目根目录（utils 的父目录的父目录）
            cache_dir = Path(__file__).parent.parent / "cache"
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._cache = Cache(str(self.cache_dir))

    def get(self, key: str, default: Any = None) -> Any:
        """
        获取缓存

        Args:
            key: 缓存键
            default: 默认值

        Returns:
            缓存值，不存在时返回默认值
        """
        return self._cache.get(key, default=default)

    def set(self, key: str, value: Any, expire: Optional[int] = None) -> None:
        """
        设置缓存

        Args:
            key: 缓存键
            value: 缓存值
            expire: 过期时间（秒），None 表示不过期
        """
        self._cache.set(key, value, expire=expire)

    def delete(self, key: str) -> bool:
        """
        删除缓存

        Args:
            key: 缓存键

        Returns:
            是否删除成功
        """
        return self._cache.delete(key)

    def clear(self) -> None:
        """清空所有缓存"""
        self._cache.clear()

    def exists(self, key: str) -> bool:
        """
        检查缓存是否存在

        Args:
            key: 缓存键

        Returns:
            是否存在
        """
        return key in self._cache

    def get_cache(self) -> Cache:
        """获取原始 Cache 实例"""
        return self._cache


# 默认缓存实例
cache = CacheManager()
