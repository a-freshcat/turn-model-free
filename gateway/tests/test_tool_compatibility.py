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

    def test_unsigned_old_tool_history_replays_native_without_text_marker(self):
        # Upstream accepts an unsigned functionCall in an already-closed round, so the
        # gateway must replay the real pair instead of writing English marker text into
        # the `model` role. That marker text is what the model started imitating.
        messages=[{'role':'user','content':'Run a tool'},
            {'role':'assistant','content':None,'tool_calls':[{'id':'old-call','type':'function',
                'function':{'name':'tool','arguments':'{"default":"keep-value","title":"keep-title"}'}}]},
            {'role':'tool','tool_call_id':'old-call','content':'old-result'},
            {'role':'user','content':'Continue'}]
        body=native_request(messages,{})
        self.assertNotIn('Historical',json.dumps(body,ensure_ascii=False))
        call=body['contents'][1]['parts'][0]
        self.assertEqual(call['functionCall']['name'],'tool')
        self.assertEqual(call['functionCall']['args']['default'],'keep-value')
        self.assertEqual(call['functionCall']['args']['title'],'keep-title')
        self.assertNotIn('thoughtSignature',call)
        self.assertIn('functionResponse',body['contents'][2]['parts'][0])
        self.assertEqual(body['contents'][2]['parts'][0]['functionResponse']['response']['result'],
                         'old-result')

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

    def test_signature_cache_reuses_a_real_call_across_account_and_model(self):
        # Measured: the same call with a signature minted on another account or another
        # model still returns 200, while the same call with no signature returns 400.
        # Bind the cache to the call, not to the credential that happened to serve it.
        from native_history import ToolSignatureCache
        cache=ToolSignatureCache(limit=1)
        cache.put('owner','model','id','tool',{'a':1},'real-signature')
        self.assertEqual(cache.get('owner','model','id','tool',{'a':1}),'real-signature')
        self.assertEqual(cache.get('other','model','id','tool',{'a':1}),'real-signature')
        self.assertEqual(cache.get('owner','other','id','tool',{'a':1}),'real-signature')
        # Arguments still isolate: a different call must not inherit a signature.
        self.assertIsNone(cache.get('owner','model','id','tool',{'a':2}))
        self.assertIsNone(cache.get('owner','model','other-id','tool',{'a':1}))
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

    def test_polluted_history_markers_are_stripped_before_replay(self):
        # Rounds already written by an older gateway still sit in the session log and are
        # replayed on every request; that is what made the model imitate the phrasing.
        polluted=(
            'Historical assistant tool call (already executed): {"tool": "read", "arguments": {"offset": 910, "limit": 45, "file_path": "C:\\\\Users\\\\handsomeboys\\\\.dsh\\\\profiles\\\\desktop\\\\node_modules\\\\dsh-our-free-model\\\\index.js"}}'
            'Historical assistant tool call (already executed): {"tool": "pwsh", "arguments": {"description": "Check listening ports on 8326 and 53760", "command": "Get-NetTCPConnection -State Listen | Where-Object { $_.LocalPort -in @(8326, 8199, 53760) } | Select-Object LocalAddress, LocalPort, OwningProcess"}}'
        )
        body=native_request([{'role':'user','content':'go'},
            {'role':'assistant','content':polluted},
            {'role':'user','content':'continue'}],{})
        wire=json.dumps(body,ensure_ascii=False)
        self.assertNotIn('Historical',wire)
        self.assertNotIn('already executed',wire)
        self.assertNotIn('Where-Object',wire)
        # The polluted turn carries nothing worth replaying, so it must vanish entirely;
        # the two surrounding user turns then merge.
        self.assertEqual([c['role'] for c in body['contents']],['user'])
        texts=[p['text'] for c in body['contents'] for p in c['parts'] if 'text' in p]
        self.assertEqual(texts,['go','continue'])

    def test_legacy_marker_strip_leaves_ordinary_history_untouched(self):
        from antigravity_driver import strip_legacy_markers
        for text in ('Just prose.','{"not":"a marker"}','Historical note: the tool ran.'):
            self.assertEqual(strip_legacy_markers(text),text)
        self.assertEqual(strip_legacy_markers(None),None)
        self.assertEqual(strip_legacy_markers(''),'')

    def test_unsupported_recursive_schema_is_an_input_error(self):
        schema={'type':'object','$defs':{'Self':{'$ref':'#/$defs/Self'}},
                'properties':{'value':{'$ref':'#/$defs/Self'}}}
        with self.assertRaises(InvalidRequest):
            native_request([{'role':'user','content':'test'}],{'tools':[{'type':'function',
                'function':{'name':'tool','parameters':schema}}]})
