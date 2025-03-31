from pathlib import Path
import yaml
from typing import Dict, Any

class ConfigManager:
    def __init__(self):
        config_path = str(Path(__file__).parent.parent / 'config' / 'config.yaml')
        self.config_path = Path(config_path)
        self._config = self._load_config()
    
    def _load_config(self) -> Dict[str, Any]:
        with open(self.config_path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    
    @property
    def database(self) -> Dict[str, Any]:
        return self._config.get('database', {})
    
    @property
    def sqlite_db_path(self) -> str:
        return self.database.get('sqlite_db_path', '')
    
    @property
    def mysql_config(self) -> Dict[str, str]:
        return {
            'user': self.database.get('mysql_user', ''),
            'password': self.database.get('mysql_password', ''),
            'host': self.database.get('mysql_host', ''),
            'database': self.database.get('mysql_database', '')
        }
    
    @property
    def db_type(self) -> str:
        return self.database.get('db_type', 'sqlite')