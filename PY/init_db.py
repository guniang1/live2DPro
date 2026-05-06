"""初始化数据库表结构"""
import os
from dotenv import load_dotenv
import pymysql

# 加载环境变量
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

def init_database():
    # 获取数据库配置
    host = os.environ.get("MYSQL_HOST", "localhost")
    port = int(os.environ.get("MYSQL_PORT", "3306"))
    user = os.environ.get("MYSQL_USER", "root")
    password = os.environ.get("MYSQL_PASSWORD", "123456")
    database = os.environ.get("MYSQL_DATABASE", "live2d")
    
    try:
        # 先连接到 MySQL（不指定数据库）
        conn = pymysql.connect(
            host=host,
            port=port,
            user=user,
            password=password,
            charset="utf8mb4"
        )
        
        cursor = conn.cursor()
        
        # 创建数据库
        cursor.execute(f"CREATE DATABASE IF NOT EXISTS {database} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;")
        print(f"✅ 数据库 {database} 已创建或已存在")
        
        # 选择数据库
        cursor.execute(f"USE {database};")
        
        # 创建表结构
        schema_path = os.path.join(os.path.dirname(__file__), "live2d_db", "schema.sql")
        if os.path.exists(schema_path):
            with open(schema_path, "r", encoding="utf-8") as f:
                sql_content = f.read()
            
            # 分割 SQL 语句并执行
            sql_statements = sql_content.split(";")
            for stmt in sql_statements:
                stmt = stmt.strip()
                if stmt:
                    try:
                        cursor.execute(stmt)
                    except pymysql.MySQLError as e:
                        if "already exists" not in str(e):
                            print(f"⚠️ 执行 SQL 时警告: {e}")
            
            print("✅ 表结构已创建")
        else:
            print("⚠️ 未找到 schema.sql 文件")
        
        conn.commit()
        cursor.close()
        conn.close()
        print("✅ 数据库初始化完成")
        
    except pymysql.MySQLError as e:
        print(f"❌ 数据库连接失败: {e}")
        raise

if __name__ == "__main__":
    init_database()