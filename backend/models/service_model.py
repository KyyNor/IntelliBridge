from typing import Dict, Optional



from sqlalchemy import Column, String, Text, Integer



from sqlalchemy.ext.declarative import declarative_base



from utils.db import DatabaseManager




Base = declarative_base()



class McpService(Base):



    __tablename__ = 'ib_services_info'
    



    id = Column(Integer, primary_key=True, autoincrement=True)  # 新增自增主键



    name = Column(String(255), unique=True)  # 修改为unique约束



    description = Column(Text)



    endpoint = Column(String(255), nullable=False)



class McpServiceModel:
    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager
        Base.metadata.create_all(bind=self.db.engine)

    def register_mcp_service(self, mcp_service: McpService):
        session = self.db.connect()

        try:
            session.merge(mcp_service)
            session.commit()

            return mcp_service
        except Exception as e:
            session.rollback()
            raise e
        finally:
            session.close()

    def list_mcp_services(self):
        session = self.db.connect()
        try:
            return session.query(McpService).all()
        finally:
            session.close()