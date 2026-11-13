from datetime import date
from sqlalchemy import ForeignKey,UniqueConstraint,Date,Float,Integer,Index
from sqlalchemy.orm import Mapped,mapped_column
from src.config.database import Base
class Observation(Base):
    __tablename__='observations'
    # 硬不变量：同一序列同一日期物理上只允许一行，任何并发同日写入都由数据库拒绝，
    # 再经导入服务按报表版本调和（跳过/更正/冲突隔离），保证不产生重复日观测。
    __table_args__=(
        UniqueConstraint('series_id','day',name='uq_series_day'),
        Index('ix_obs_series_day_ver','series_id','day','report_version'),
    )
    id:Mapped[int]=mapped_column(primary_key=True)
    series_id:Mapped[int]=mapped_column(ForeignKey('series.id'))
    day:Mapped[date]=mapped_column(Date)
    value:Mapped[float]=mapped_column(Float)
    # 供应方报表版本：未携带版本的历史观测记 0；越大越新
    report_version:Mapped[int]=mapped_column(Integer,default=0,server_default='0')
    # 最近一次写入该观测的导入批次，便于追溯
    batch_id:Mapped[int|None]=mapped_column(ForeignKey('import_batches.id'),nullable=True)
