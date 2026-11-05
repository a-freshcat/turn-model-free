"""Translate tool schemas only; never traverse tool arguments or tool results."""
import copy
import json

from account_manager import InvalidRequest


def clean_tool_schema(schema):
    if not isinstance(schema,dict):
        raise InvalidRequest('工具 parameters 必须是 JSON Schema 对象。')
    root=schema
    nodes=0

    def merge(left,right):
        out=copy.deepcopy(left)
        for key,value in right.items():
            if key=='properties':out[key]={**out.get(key,{}),**value}
            elif key=='required':out[key]=list(dict.fromkeys(out.get(key,[])+value))
            elif key=='description' and out.get(key):out[key]+='\n'+str(value)
            elif key=='type' and out.get(key) not in (None,value):
                raise InvalidRequest('工具 Schema allOf 包含冲突类型。')
            else:out[key]=copy.deepcopy(value)
        return out

    def walk(node,depth=0,refs=()):
        nonlocal nodes
        nodes+=1
        if depth>48 or nodes>20000:
            raise InvalidRequest('工具 Schema 太复杂，请简化嵌套或引用。')
        if not isinstance(node,dict):
            raise InvalidRequest('工具 Schema 节点必须是对象。')
        node=copy.deepcopy(node)
        ref=node.pop('$ref',None)
        if ref is not None:
            if not isinstance(ref,str) or not ref.startswith('#/') or ref in refs:
                raise InvalidRequest('暂不支持工具 Schema 的外部或循环引用。')
            target=root
            try:
                for part in ref[2:].split('/'):
                    target=target[part.replace('~1','/').replace('~0','~')]
            except (KeyError,TypeError):
                raise InvalidRequest('工具 Schema 引用不存在。') from None
            return merge(walk(target,depth+1,refs+(ref,)),walk(node,depth+1,refs+(ref,)))
        if 'allOf' in node:
            branches=node.pop('allOf')
            if not isinstance(branches,list):raise InvalidRequest('工具 allOf 必须是数组。')
            out={}
            for branch in branches:out=merge(out,walk(branch,depth+1,refs))
            return merge(out,walk(node,depth+1,refs))
        if any(key in node for key in ('if','then','else','not','patternProperties','dependentSchemas')):
            raise InvalidRequest('工具 Schema 包含当前驱动不支持的条件约束，请简化该工具声明。')
        out={}
        description=node.get('description')
        hints=[]
        if isinstance(description,str):out['description']=description
        kind=node.get('type')
        if isinstance(kind,list):
            choices=[item for item in kind if item!='null']
            if 'null' in kind:out['nullable']=True
            if len(choices)==1:kind=choices[0]
            elif choices:
                out['anyOf']=[{'type':item} for item in choices]
                kind=None
        if kind=='null':kind='string';out['nullable']=True;hints.append('Only null is permitted.')
        if kind is not None:
            if kind not in ('object','array','string','integer','number','boolean'):
                raise InvalidRequest('工具 Schema type 无效。')
            out['type']=kind
        if isinstance(node.get('nullable'),bool):out['nullable']=node['nullable']
        properties=node.get('properties')
        if properties is not None:
            if not isinstance(properties,dict):raise InvalidRequest('工具 properties 必须是对象。')
            out['properties']={name:walk(child,depth+1,refs) for name,child in properties.items()}
            out.setdefault('type','object')
        required=node.get('required')
        if required is not None:
            if not isinstance(required,list) or any(not isinstance(name,str) for name in required):
                raise InvalidRequest('工具 required 必须是字段名数组。')
            known=out.get('properties',{})
            out['required']=[name for name in required if name in known]
        if node.get('items') is not None:
            if not isinstance(node['items'],dict):raise InvalidRequest('暂不支持元组形式的工具数组。')
            out['items']=walk(node['items'],depth+1,refs)
        elif out.get('type')=='array':
            out['items']={'type':'string'}
        for key in ('anyOf','oneOf'):
            if key in node:
                if not isinstance(node[key],list):raise InvalidRequest('工具联合类型必须是数组。')
                out['anyOf']=[walk(child,depth+1,refs) for child in node[key]]
        for key in ('enum','const','minimum','maximum','minLength','maxLength','pattern',
                    'minItems','maxItems','format','additionalProperties'):
            if key in node:
                hints.append(key+': '+json.dumps(node[key],ensure_ascii=False,separators=(',',':')))
        if hints:out['description']='\n'.join(filter(None,[out.get('description',''),*hints]))
        return out

    return walk(schema)
