"""数据库连接配置（可通过环境变量覆盖）。"""

import os
from dataclasses import dataclass


@dataclass
class DbConfig:
    host: str = "127.0.0.1"
    port: int = 3306
    user: str = "root"
    password: str = ""
    database: str = "live2d_digital_human"
    charset: str = "utf8mb4"

    @classmethod
    def from_env(cls) -> "DbConfig":
        return cls(
            host=os.environ.get("MYSQL_HOST", "127.0.0.1"),
            port=int(os.environ.get("MYSQL_PORT", "3306")),
            user=os.environ.get("MYSQL_USER", "root"),
            password=os.environ.get("MYSQL_PASSWORD", ""),
            database=os.environ.get("MYSQL_DATABASE", "live2d_digital_human"),
            charset=os.environ.get("MYSQL_CHARSET", "utf8mb4"),
        )
