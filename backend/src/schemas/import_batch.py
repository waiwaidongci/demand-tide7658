from pydantic import BaseModel,Field
class ImportPreviewInput(BaseModel):
    content:str=Field(min_length=1)
class ImportCommitInput(BaseModel):
    content:str=Field(min_length=1)
    idem_key:str=Field(min_length=1,max_length=80)
    filename:str|None=Field(default=None,max_length=160)
