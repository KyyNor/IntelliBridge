import yaml
from pathlib import Path
from typing import Any, Dict, Optional


class ConfigManager:
    """配置文件管理工具类"""

    def __init__(self, config_path: str = None):
        """
        初始化配置管理器

        Args:
            config_path: 配置文件路径，默认为项目根目录下的 config/config.yaml
        """
        if config_path is None:
            # 获取项目根目录
            config_path = Path(__file__).parent.parent / "config" / "config.yaml"
        self.config_path = Path(config_path)
        self._config: Optional[Dict[str, Any]] = None

    def load_config(self) -> Dict[str, Any]:
        """
        加载配置文件

        Returns:
            配置字典
        """
        if not self.config_path.exists():
            raise FileNotFoundError(f"配置文件不存在: {self.config_path}")

        with open(self.config_path, 'r', encoding='utf-8') as f:
            self._config = yaml.safe_load(f) or {}

        return self._config

    def get_config(self) -> Dict[str, Any]:
        """
        获取配置（缓存）

        Returns:
            配置字典
        """
        if self._config is None:
            self._config = self.load_config()
        return self._config

    def get(self, key: str, default: Any = None) -> Any:
        """
        获取配置项（支持嵌套键，如 "database.host"）

        Args:
            key: 配置键，支持点号分隔的嵌套键
            default: 默认值

        Returns:
            配置值
        """
        config = self.get_config()
        keys = key.split('.')

        value = config
        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default

        return value

    def reload(self) -> Dict[str, Any]:
        """
        重新加载配置文件

        Returns:
            配置字典
        """
        self._config = None
        return self.load_config()

    def save_config(self, config: Dict[str, Any]) -> None:
        """
        保存配置到文件

        Args:
            config: 要保存的配置字典
        """
        self.config_path.parent.mkdir(parents=True, exist_ok=True)

        with open(self.config_path, 'w', encoding='utf-8') as f:
            yaml.dump(config, f, allow_unicode=True, default_flow_style=False)

        self._config = config


# 默认配置管理器实例
config = ConfigManager()
