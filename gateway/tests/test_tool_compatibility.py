import copy
import json
import unittest

from antigravity_driver import native_request
from account_manager import InvalidRequest


class ToolCompatibilityTests(unittest.TestCase):
    def test_dsh_schema_metadata_is_removed_without_changing_property_names(self):
        schema={'$schema':'http://json-schema.org/draft-07/schema#','title':'Tool input',
            'type':'object','properties':{
                'default':{'type':'string','default':'x'},
                'title':{'type':'string'},'nullable':{'type':['string','null']},
                'options':{'type':'object','additionalProperties':False}},
            'required':['default'],'additionalProperties':False}
        original=copy.deepcopy(schema)
        body=native_request([{'role':'user','content':'test'}],{'tools':[{'type':'function',
            'function':{'name':'tool','parameters':schema}}]})
        cleaned=body['tools'][0]['functionDeclarations'][0]['parameters']
        self.assertNotIn('$schema',cleaned)
        self.assertNotIn('title',cleaned)
        self.assertNotIn('additionalProperties',cleaned)
        self.assertEqual(set(cleaned['properties']),set(schema['properties']))
        self.assertNotIn('default',cleaned['properties']['default'])
        self.assertEqual(cleaned['properties']['nullable']['type'],'string')
        self.assertTrue(cleaned['properties']['nullable']['nullable'])
        self.assertEqual(schema,original)

    def test_local_refs_are_resolved_before_definitions_are_removed(self):
        schema={'type':'object','$defs':{'Value':{'type':'string'}},
                'properties':{'value':{'$ref':'#/$defs/Value'}}}
        body=native_request([{'role':'user','content':'test'}],{'tools':[{'type':'function',
            'function':{'name':'tool','parameters':schema}}]})
        cleaned=body['tools'][0]['functionDeclarations'][0]['parameters']
        self.assertNotIn('$defs',cleaned)
        self.assertEqual(cleaned['properties']['value']['type'],'string')

    def test_unsigned_old_tool_history_is_kept_as_explicit_history_not_fabricated_signature(self):
        messages=[{'role':'user','content':'Run a tool'},
            {'role':'assistant','content':None,'tool_calls':[{'id':'old-call','type':'function',
                'function':{'name':'tool','arguments':'{"default":"keep-value","title":"keep-title"}'}}]},
            {'role':'tool','tool_call_id':'old-call','content':'old-result'},
            {'role':'user','content':'Continue'}]
        body=native_request(messages,{})
        parts=[part for item in body['contents'] for part in item['parts']]
        self.assertFalse(any('functionCall' in part or 'functionResponse' in part for part in parts))
        text='\n'.join(part.get('text','') for part in parts)
        self.assertIn('old-result',text)
        self.assertIn('keep-value',text)
        self.assertIn('keep-title',text)
        self.assertNotIn('thoughtSignature',json.dumps(body))

    def test_signed_history_remains_native_and_arguments_are_not_schema_cleaned(self):
        body=native_request([{'role':'user','content':'test'},
            {'role':'assistant','content':None,'tool_calls':[{'id':'signed','type':'function',
                'function':{'name':'tool','arguments':'{"default":"keep"}'},
                'extra_content':{'google':{'thought_signature':'actual-upstream-signature'}}}]},
            {'role':'tool','tool_call_id':'signed','content':'result'}],{})
        call=body['contents'][1]['parts'][0]
        self.assertEqual(call['thoughtSignature'],'actual-upstream-signature')
        self.assertEqual(call['functionCall']['args']['default'],'keep')
        self.assertIn('functionResponse',body['contents'][2]['parts'][0])

    def test_real_signature_cache_is_scoped_to_account_model_and_arguments(self):
        from native_history import ToolSignatureCache
        cache=ToolSignatureCache(limit=1)
        cache.put('owner','model','id','tool',{'a':1},'real-signature')
        self.assertEqual(cache.get('owner','model','id','tool',{'a':1}),'real-signature')
        self.assertIsNone(cache.get('other','model','id','tool',{'a':1}))
        self.assertIsNone(cache.get('owner','other','id','tool',{'a':1}))
        self.assertIsNone(cache.get('owner','model','id','tool',{'a':2}))
        messages=[{'role':'user','content':'test'},
            {'role':'assistant','content':None,'tool_calls':[{'id':'id','type':'function',
                'function':{'name':'tool','arguments':'{"a":1}'}}]},
            {'role':'tool','tool_call_id':'id','content':'result'}]
        body=native_request(messages,{},signature_lookup=lambda id,name,args:
            cache.get('owner','model',id,name,args))
        self.assertEqual(body['contents'][1]['parts'][0]['thoughtSignature'],'real-signature')
        self.assertIn('functionResponse',body['contents'][2]['parts'][0])
        cache.put('owner','model','new','tool',{},'another-real-signature')
        self.assertIsNone(cache.get('owner','model','id','tool',{'a':1}))

    def test_unsupported_recursive_schema_is_an_input_error(self):
        schema={'type':'object','$defs':{'Self':{'$ref':'#/$defs/Self'}},
                'properties':{'value':{'$ref':'#/$defs/Self'}}}
        with self.assertRaises(InvalidRequest):
            native_request([{'role':'user','content':'test'}],{'tools':[{'type':'function',
                'function':{'name':'tool','parameters':schema}}]})
