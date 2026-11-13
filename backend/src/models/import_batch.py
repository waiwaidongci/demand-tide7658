from datetime import datetime
from sqlalchemy import String,Integer,JSON,DateTime,UniqueConstraint,Text
from sqlalchemy.orm import Mapped,mapped_column
from src.config.database import Base
class ImportBatch(Base):
    __tablename__='import_batches'
    # 幂等键由调用方给出；同键重放/并发提交只返回首批结果，不新增观测
    __table_args__=(UniqueConstraint('idem_key',name='uq_import_idem_key'),)
    id:Mapped[int]=mapped_column(primary_key=True)
    series_id:Mapped[int]=mapped_column(Integer)
    idem_key:Mapped[str]=mapped_column(String(80))
    filename:Mapped[str|None]=mapped_column(String(160),nullable=True)
    source_file:Mapped[str|None]=mapped_column(String(120),nullable=True)
    status:Mapped[str]=mapped_column(String(20),default='committed')
    total_rows:Mapped[int]=mapped_column(Integer,default=0)
    summary:Mapped[dict]=mapped_column(JSON,default=dict)
    result:Mapped[dict]=mapped_column(JSON,default=dict)
    created_by:Mapped[int]=mapped_column(Integer)
    created_at:Mapped[datetime]=mapped_column(DateTime,default=datetime.utcnow)
    note:Mapped[str|None]=mapped_column(Text,nullable=True)
