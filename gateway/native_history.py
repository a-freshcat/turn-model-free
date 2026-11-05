"""Bounded in-memory replay of real upstream signatures, scoped to exact calls."""
import hashlib
import json
import time
from collections import OrderedDict


class ToolSignatureCache:
    def __init__(self,limit=512,ttl=3600):
        self.entries=OrderedDict()
        self.limit,self.ttl=limit,ttl

    def key(self,owner,model,call_id,name,args):
        digest=hashlib.sha256(json.dumps(args,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).digest()
        return owner,model,call_id,name,digest

    def put(self,owner,model,call_id,name,args,signature):
        if not isinstance(signature,str) or not signature or len(signature)>65536:return
        key=self.key(owner,model,call_id,name,args)
        self.entries[key]=(time.monotonic()+self.ttl,signature)
        self.entries.move_to_end(key)
        while len(self.entries)>self.limit:self.entries.popitem(last=False)

    def get(self,owner,model,call_id,name,args):
        key=self.key(owner,model,call_id,name,args)
        found=self.entries.get(key)
        if found is None:return None
        if found[0]<=time.monotonic():
            self.entries.pop(key,None)
            return None
        self.entries.move_to_end(key)
        return found[1]
