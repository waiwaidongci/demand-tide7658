from datetime import date
from pydantic import BaseModel,Field
class ObservationInput(BaseModel):
    day:date
    value:float=Field(ge=0,le=10000000,allow_inf_nan=False)
    report_version:int=Field(default=0,ge=0,le=2147483647)
