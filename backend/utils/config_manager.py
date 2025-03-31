from pathlib import Path
import yaml
from typing import Dict, Any
from utils.logger import log

class ConfigManager:
    def __init__(self):
        config_path = str(Path(__file__).parent.parent / 'config' / 'config.yaml')
        self.config_path = Path(config_path)
        log.info(f"初始化配置管理器，配置文件路径: {config_path}")
        self._config = self._load_config()
    
    def _load_config(self) -> Dict[str, Any]:
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)
                log.debug(f"成功加载配置文件: {self.config_path}")
                return config
        except Exception as e:
            log.error(f"加载配置文件失败: {str(e)}")
            raise
    
    @property
    def database(self) -> Dict[str, Any]:
        return self._config.get('database', {})
    
    @property
    def sqlite_db_path(self) -> str:
        path = self.database.get('sqlite_db_path', '')
        log.debug(f"获取SQLite数据库路径: {path}")
        return path
    
    @property
    def mysql_config(self) -> Dict[str, str]:
        config = {
            'user': self.database.get('mysql_user', ''),
            'password': self.database.get('mysql_password', ''),
            'host': self.database.get('mysql_host', ''),
            'database': self.database.get('mysql_database', '')
        }
        log.debug(f"获取MySQL配置: {config['host']}/{config['database']}")
        return config
    
    @property
    def db_type(self) -> str:
        db_type = self.database.get('db_type', 'sqlite')
        log.debug(f"获取数据库类型: {db_type}")
        return db_type